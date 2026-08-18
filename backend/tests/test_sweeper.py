"""Layer 5 against the database: the unmatched sweeper and the exception queue.

``unmatched_after_s`` is 1 in the test settings, so these run in real time without
waiting five minutes.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import func, select, update

from ledgerloop.db.enums import MatchLayer, ReconStatus
from ledgerloop.db.models import Exception_, GatewayTransaction, LedgerEntry, ReconciliationResult
from ledgerloop.worker.sweeper import Sweeper
from tests.helpers import gateway_payload, ingest_pair, ledger_payload, post_gateway, post_ledger
from tests.pipeline import run_pipeline


async def _age_rows(session, seconds: int = 10) -> None:  # type: ignore[no-untyped-def]
    """Backdate received_at so the sweeper's window has elapsed.

    Faster and far more reliable than sleeping, and it exercises the same predicate the
    production sweeper uses.
    """
    for model in (GatewayTransaction, LedgerEntry):
        await session.execute(
            update(model).values(
                received_at=func.now() - func.make_interval(0, 0, 0, 0, 0, 0, seconds)
            )
        )
    await session.commit()


async def _results(session):  # type: ignore[no-untyped-def]
    return list(
        (await session.execute(select(ReconciliationResult).order_by(ReconciliationResult.id)))
        .scalars()
        .all()
    )


async def test_gateway_without_a_counterparty_becomes_unmatched(api, sessions, settings, session):
    await post_gateway(api, gateway_payload("TXN-S1"))
    await _age_rows(session)

    assert await Sweeper(sessions, settings).sweep_once() == 1

    result = (await session.execute(select(ReconciliationResult))).scalar_one()
    assert result.status is ReconStatus.UNMATCHED_GATEWAY_ONLY
    assert result.match_layer is MatchLayer.UNMATCHED_SWEEP
    assert result.ledger_entry_id is None


async def test_ledger_without_a_counterparty_becomes_unmatched(api, sessions, settings, session):
    await post_ledger(api, ledger_payload("TXN-S2"))
    await _age_rows(session)

    await Sweeper(sessions, settings).sweep_once()

    result = (await session.execute(select(ReconciliationResult))).scalar_one()
    assert result.status is ReconStatus.UNMATCHED_LEDGER_ONLY
    assert result.gateway_txn_id is None


async def test_sweeping_opens_an_exception(api, sessions, settings, session):
    await post_gateway(api, gateway_payload("TXN-S3"))
    await _age_rows(session)
    await Sweeper(sessions, settings).sweep_once()

    exception = (await session.execute(select(Exception_))).scalar_one()
    assert exception.closed_at is None
    assert exception.opened_at is not None


async def test_rows_inside_the_window_are_left_alone(api, sessions, settings, session):
    """Just arrived. Declaring it a break now would report a transaction whose ledger
    side is still perfectly on time."""
    await post_gateway(api, gateway_payload("TXN-S4"))

    assert await Sweeper(sessions, settings).sweep_once() == 0
    assert (
        await session.execute(select(func.count()).select_from(ReconciliationResult))
    ).scalar_one() == 0


async def test_sweeper_marks_the_row_reconciled(api, sessions, settings, session):
    await post_gateway(api, gateway_payload("TXN-S5"))
    await _age_rows(session)
    await Sweeper(sessions, settings).sweep_once()

    row = (await session.execute(select(GatewayTransaction))).scalar_one()
    assert row.reconciled_at is not None


async def test_a_second_sweep_changes_nothing(api, sessions, settings, session):
    """Idempotent by the same mechanism as the worker: the unique index refuses the
    second write, so a sweeper restart mid-pass cannot double-report breaks."""
    await post_gateway(api, gateway_payload("TXN-S6"))
    await _age_rows(session)
    sweeper = Sweeper(sessions, settings)

    assert await sweeper.sweep_once() == 1
    assert await sweeper.sweep_once() == 0
    assert len(await _results(session)) == 1
    assert (await session.execute(select(func.count()).select_from(Exception_))).scalar_one() == 1


async def test_sweeper_matches_before_giving_up(api, sessions, settings, session):
    """The repair path. Both sides are present and matchable but no stream message was
    ever processed -- the sweeper must reconcile them, not report two false breaks."""
    await ingest_pair(api, "TXN-S7")
    await _age_rows(session)

    assert await Sweeper(sessions, settings).sweep_once() == 1

    results = await _results(session)
    assert len(results) == 1
    assert results[0].status is ReconStatus.MATCHED
    assert results[0].match_layer is MatchLayer.EXACT
    assert (await session.execute(select(func.count()).select_from(Exception_))).scalar_one() == 0


async def test_sweeper_recovers_a_drifted_pair_as_drift(api, sessions, settings, session):
    await ingest_pair(api, "TXN-S8", gateway_amount="1000.00", ledger_amount="1005.00")
    await _age_rows(session)
    await Sweeper(sessions, settings).sweep_once()

    result = (await session.execute(select(ReconciliationResult))).scalar_one()
    assert result.status is ReconStatus.AMOUNT_DRIFT


async def test_sweeper_ignores_already_reconciled_rows(api, sessions, stream, settings, session):
    await ingest_pair(api, "TXN-S9")
    await run_pipeline(sessions, stream, settings)
    await _age_rows(session)

    assert await Sweeper(sessions, settings).sweep_once() == 0
    assert len(await _results(session)) == 1


async def test_sweeper_reports_both_sides_when_they_cannot_be_paired(
    api, sessions, settings, session
):
    """Same txn_id, amounts far beyond tolerance: two honest breaks, not one invented
    pairing."""
    await ingest_pair(api, "TXN-S10", gateway_amount="1000.00", ledger_amount="5000.00")
    await _age_rows(session)

    assert await Sweeper(sessions, settings).sweep_once() == 2

    statuses = {result.status for result in await _results(session)}
    assert statuses == {
        ReconStatus.UNMATCHED_GATEWAY_ONLY,
        ReconStatus.UNMATCHED_LEDGER_ONLY,
    }
    assert (await session.execute(select(func.count()).select_from(Exception_))).scalar_one() == 2


async def test_run_forever_stops_promptly_on_the_stop_event(api, sessions, settings, session):
    """SIGTERM handling: the loop waits on the stop event rather than sleeping, so
    shutdown does not take up to a full sweep interval."""
    await post_gateway(api, gateway_payload("TXN-S11"))
    await _age_rows(session)

    stop = asyncio.Event()
    task = asyncio.create_task(Sweeper(sessions, settings).run_forever(stop))
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert len(await _results(session)) == 1

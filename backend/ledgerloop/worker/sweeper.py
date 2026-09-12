"""Layer 5: the unmatched sweeper.

Runs every 30s and asks one question of each side: is anything still waiting for a
counterparty past the configured window (default 5 minutes)?

It does two things per stale row, in this order, and the order matters:

1. **One last matching attempt.** The sweeper is also the repair path. If a stream
   message was lost, or a worker died between reading and acking in a way that
   outlived the pending-list recovery, the row is still sitting in Postgres with a
   perfectly good counterparty next to it. Declaring it a break without looking would
   manufacture an exception for a transaction that reconciles fine.
2. **Only then, give up.** ``unmatched_gateway_only`` / ``unmatched_ledger_only``, and
   open an exception for a human.

This ordering is why the engine's unmatched count can be trusted: every unmatched row
was checked against live data at the moment it was declared unmatched.

The same loop also carries demo-tenant retention, on a much slower clock. It lives
here rather than in a loop of its own because there is already a resident process with
a working shutdown protocol, and a second one would be a second thing to deploy,
supervise and drain for work that runs once an hour and usually deletes nothing.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ledgerloop.config import Settings
from ledgerloop.db.enums import AccountKind, IngestSource
from ledgerloop.db.models import (
    Account,
    Exception_,
    GatewayTransaction,
    LedgerEntry,
    ReconciliationResult,
)
from ledgerloop.matching.core import MatchConfig, decide, decide_unmatched
from ledgerloop.observability.logging import get_logger
from ledgerloop.observability.metrics import (
    SWEEPER_MARKED_TOTAL,
    SWEEPER_RUNS_TOTAL,
    record_error,
)
from ledgerloop.worker.persist import (
    apply_decision,
    compute_latency_ms,
    find_counterparties,
    find_stale_pending,
)

log = get_logger("ledgerloop.sweeper")


class Sweeper:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        batch_limit: int = 500,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._config = MatchConfig.from_settings(settings)
        self._batch_limit = batch_limit
        #: Monotonic timestamp of the last retention pass. Starts at 0 so the first
        #: sweep after a restart runs one, rather than waiting out a full interval.
        self._last_demo_sweep = 0.0

    async def sweep_once(self) -> int:
        """One pass over both sides. Returns how many rows reached a terminal state."""
        window = timedelta(seconds=self._settings.unmatched_after_s)
        resolved = 0

        for source in (IngestSource.GATEWAY, IngestSource.LEDGER):
            async with self._sessionmaker() as session:
                stale = await find_stale_pending(session, source, window, self._batch_limit)

            for candidate in stale:
                async with self._sessionmaker() as session:
                    # Isolation: READ COMMITTED (PostgreSQL default). Each row is its
                    # own transaction so one contended row cannot stall the whole
                    # sweep, and the unique indexes still prevent a double write if a
                    # matcher resolves the same row concurrently.
                    async with session.begin():
                        counterparties = await find_counterparties(session, candidate, self._config)
                        decision = decide(candidate, counterparties, self._config)
                        sides = [candidate]
                        if decision is not None:
                            partner_id = (
                                decision.ledger_row_id
                                if source is IngestSource.GATEWAY
                                else decision.gateway_row_id
                            )
                            sides += [f for f in counterparties if f.row_id == partner_id]
                        else:
                            waited = datetime.now(UTC) - candidate.received_at
                            decision = decide_unmatched(candidate, waited)

                        persisted = await apply_decision(
                            session,
                            decision,
                            tenant_id=candidate.tenant_id,
                            latency_ms=compute_latency_ms(decision, sides, datetime.now(UTC)),
                            message_id=None,
                        )

                if persisted.written:
                    resolved += 1
                    SWEEPER_MARKED_TOTAL.labels(status=decision.status.value).inc()
                    log.info(
                        "sweeper.resolved",
                        txn_id=candidate.txn_id,
                        source=source.value,
                        row_id=candidate.row_id,
                        decision=decision.status.value,
                        layer=decision.layer.value,
                    )

        SWEEPER_RUNS_TOTAL.inc()
        await self._maybe_sweep_demo_accounts()
        return resolved

    # --- demo retention ---------------------------------------------------
    async def _maybe_sweep_demo_accounts(self) -> None:
        """Run retention at most once per ``demo_sweep_interval_s``.

        Rate-limited against the *sweep* loop rather than given its own schedule: the
        unmatched sweep runs every 30 seconds because it is answering a question about
        money, and retention has a 24-hour deadline. Checking a monotonic clock here
        is cheaper than 119 pointless scans of ``accounts`` an hour standing between
        the matcher and its work.
        """
        now = time.monotonic()
        if now - self._last_demo_sweep < self._settings.demo_sweep_interval_s:
            return
        self._last_demo_sweep = now
        try:
            deleted = await self.sweep_demo_accounts()
            if deleted:
                log.info("sweeper.demo_accounts_deleted", accounts=deleted)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- housekeeping must not stop matching
            record_error("sweeper", exc)
            log.error("sweeper.demo_retention_failed", error=str(exc), exc_info=True)

    async def sweep_demo_accounts(self) -> int:
        """Delete idle demo tenants and everything they own. Returns accounts removed.

        ``real`` accounts are never touched, whatever their last_seen_at says: a
        paying tenant who has not logged in for a month still owns their history.

        Deletion is explicit and ordered rather than an ``ON DELETE CASCADE`` from
        ``accounts``, because the raw tables are referenced by ``reconciliation_results``
        with ``ON DELETE RESTRICT`` -- reconciliation evidence must not vanish from
        under a result. So the order below is the dependency order, and if a future
        table is added and forgotten here, the final ``DELETE FROM accounts`` fails
        loudly on its foreign key instead of half-erasing a tenant.

        ``outbox_events`` is deliberately not touched. It holds no tenant column -- its
        rows are pointers, published within milliseconds and irrelevant long before a
        24-hour retention window closes -- and an unpublished straggler pointing at a
        deleted row is already handled: the matcher logs ``worker.row_missing``, acks,
        and moves on. Deleting by ``(source, row_id)`` per account would be a scan of
        the whole table to avoid a log line.
        """
        cutoff = datetime.now(UTC) - timedelta(hours=self._settings.demo_retention_hours)

        async with self._sessionmaker() as session:
            stale = list(
                (
                    await session.execute(
                        select(Account.id, Account.name)
                        .where(Account.kind == AccountKind.DEMO, Account.last_seen_at < cutoff)
                        .order_by(Account.last_seen_at)
                        .limit(self._batch_limit)
                    )
                ).all()
            )

        deleted = 0
        for account_id, name in stale:
            async with self._sessionmaker() as session:
                # One transaction per account, like the row loop above: a tenant whose
                # delete contends with a live matcher must not stall retention for
                # everyone else, and a partial delete of one account is impossible
                # because its five statements share a transaction.
                async with session.begin():
                    for model in (
                        Exception_,
                        ReconciliationResult,
                        GatewayTransaction,
                        LedgerEntry,
                    ):
                        await session.execute(
                            delete(model).where(model.tenant_id == account_id)
                        )
                    await session.execute(delete(Account).where(Account.id == account_id))
            deleted += 1
            log.info("sweeper.demo_account_deleted", account_id=account_id, account=name)

        return deleted

    async def run_forever(self, stop: asyncio.Event) -> None:
        log.info(
            "sweeper.started",
            interval_s=self._settings.sweep_interval_s,
            unmatched_after_s=self._settings.unmatched_after_s,
        )
        while not stop.is_set():
            try:
                resolved = await self.sweep_once()
                if resolved:
                    log.info("sweeper.pass_complete", resolved=resolved)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                record_error("sweeper", exc)
                log.error("sweeper.pass_failed", error=str(exc), exc_info=True)

            # Waiting on the stop event rather than sleeping means SIGTERM is honoured
            # immediately instead of up to 30 seconds later.
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._settings.sweep_interval_s)
            except TimeoutError:
                pass
        log.info("sweeper.stopped")

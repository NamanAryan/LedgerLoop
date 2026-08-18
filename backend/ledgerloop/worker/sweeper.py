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
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ledgerloop.config import Settings
from ledgerloop.db.enums import IngestSource
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
        return resolved

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

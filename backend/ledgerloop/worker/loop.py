"""The matcher worker's consume loop.

Delivery is at-least-once and the worker leans on that rather than fighting it:

* XACK happens *after* the decision is committed. A crash between processing and ack
  leaves the entry in the group's pending list, and either this consumer's next
  XAUTOCLAIM pass or a sibling worker's picks it up.
* Re-processing is therefore normal, not exceptional. It is safe because the write
  collapses to a no-op against the partial unique indexes -- see worker/persist.py.

The consequence worth stating in an interview: the system is at-least-once at the
transport layer and effectively-once at the data layer, and the second property is
enforced by the database rather than by hoping the first never misfires.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from redis.exceptions import ResponseError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ledgerloop.config import Settings
from ledgerloop.db.enums import MatchLayer, ReconStatus
from ledgerloop.matching.core import Decision, MatchConfig, TxnFacts, decide, decide_duplicate
from ledgerloop.observability.logging import get_logger
from ledgerloop.observability.metrics import (
    MATCH_LATENCY_SECONDS,
    MATCHER_DURATION_SECONDS,
    MATCHER_MESSAGES_TOTAL,
    QUEUE_DEPTH,
    QUEUE_PENDING,
    record_error,
)
from ledgerloop.queue.messages import IngestMessage
from ledgerloop.queue.streams import StreamClient
from ledgerloop.worker.persist import (
    apply_decision,
    compute_latency_ms,
    find_counterparties,
    load_facts,
)

log = get_logger("ledgerloop.worker")


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    decision: Decision | None
    written: bool
    duration_ms: float
    latency_ms: int | None

    @property
    def label(self) -> tuple[str, str]:
        if self.decision is None:
            return ("deferred", "deferred")
        return (self.decision.status.value, self.decision.layer.value)


class MatcherWorker:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        stream: StreamClient,
        settings: Settings,
        consumer_name: str,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._stream = stream
        self._settings = settings
        self._config = MatchConfig.from_settings(settings)
        self.consumer_name = consumer_name
        self._last_claim = 0.0

    # --- one message ------------------------------------------------------
    async def process(
        self, message: IngestMessage, message_id: str | None = None
    ) -> ProcessOutcome:
        started = time.perf_counter()

        async with self._sessionmaker() as session:
            # Isolation: READ COMMITTED (PostgreSQL default). The matcher reads the
            # counterparty side and writes one result; correctness against concurrent
            # workers comes from the unique indexes, not from snapshot isolation.
            async with session.begin():
                candidate = await load_facts(session, message.source, message.row_id)
                if candidate is None:
                    # The row is gone (or never committed). Nothing to reconcile; ack
                    # and move on rather than retrying forever on a phantom.
                    log.warning(
                        "worker.row_missing",
                        message_id=message_id,
                        source=message.source.value,
                        row_id=message.row_id,
                    )
                    return ProcessOutcome(None, False, (time.perf_counter() - started) * 1000, None)

                sides: list[TxnFacts] = [candidate]
                if message.is_duplicate:
                    decision: Decision | None = decide_duplicate(candidate, message.submissions)
                else:
                    counterparties = await find_counterparties(session, candidate, self._config)
                    decision = decide(candidate, counterparties, self._config)
                    if decision is not None:
                        partner_id = (
                            decision.ledger_row_id
                            if candidate.side.value == "gateway"
                            else decision.gateway_row_id
                        )
                        sides += [f for f in counterparties if f.row_id == partner_id]

                if decision is None:
                    # Deferred: the counterparty has not arrived yet. Write nothing --
                    # an "unmatched" row here would be a guess, and the sweeper is the
                    # component whose job is to decide when waiting has gone on too long.
                    outcome = ProcessOutcome(
                        None, False, (time.perf_counter() - started) * 1000, None
                    )
                else:
                    latency_ms = compute_latency_ms(decision, sides, datetime.now(UTC))
                    persisted = await apply_decision(
                        session, decision, latency_ms=latency_ms, message_id=message_id
                    )
                    outcome = ProcessOutcome(
                        decision,
                        persisted.written,
                        (time.perf_counter() - started) * 1000,
                        latency_ms,
                    )

        status_label, layer_label = outcome.label
        MATCHER_MESSAGES_TOTAL.labels(status=status_label, layer=layer_label).inc()
        MATCHER_DURATION_SECONDS.observe(outcome.duration_ms / 1000)
        if outcome.written and outcome.latency_ms is not None:
            MATCH_LATENCY_SECONDS.observe(outcome.latency_ms / 1000)

        log.info(
            "worker.processed",
            message_id=message_id,
            txn_id=message.txn_id,
            source=message.source.value,
            row_id=message.row_id,
            decision=status_label,
            layer=layer_label,
            written=outcome.written,
            duration_ms=round(outcome.duration_ms, 3),
            latency_ms=outcome.latency_ms,
        )
        return outcome

    # --- the loop ---------------------------------------------------------
    async def run(self, stop: asyncio.Event) -> None:
        await self._stream.ensure_group()
        log.info(
            "worker.started",
            consumer=self.consumer_name,
            group=self._stream.group,
            stream=self._stream.key,
        )

        while not stop.is_set():
            try:
                entries = await self._maybe_claim()
                entries += await self._stream.read(
                    self.consumer_name,
                    count=self._settings.stream_batch_size,
                    block_ms=self._settings.stream_block_ms,
                )

                for entry_id, message in entries:
                    await self.process(message, entry_id)
                    # Ack only after the decision is committed. Acking first would
                    # turn a crash into a permanently unreconciled transaction.
                    await self._stream.ack(entry_id)
                    if stop.is_set():
                        # Graceful shutdown: the message in hand is finished and acked
                        # before we leave, so nothing is abandoned mid-flight.
                        break

                await self._refresh_gauges()
            except asyncio.CancelledError:
                raise
            except ResponseError as exc:
                # NOGROUP: the stream or its consumer group is gone -- a Redis restart
                # without persistence, or an operator clearing the key. Recreate it and
                # carry on. Postgres still holds every row, and the sweeper reconciles
                # anything whose message was lost with it.
                if "NOGROUP" in str(exc):
                    log.warning("worker.group_missing_recreating", error=str(exc))
                    await self._stream.ensure_group()
                    continue
                record_error("worker", exc)
                log.error("worker.batch_failed", error=str(exc), exc_info=True)
                await asyncio.sleep(0.5)
            except Exception as exc:  # noqa: BLE001 -- one bad batch must not kill the worker
                record_error("worker", exc)
                log.error("worker.batch_failed", error=str(exc), exc_info=True)
                await asyncio.sleep(0.5)

        log.info("worker.stopped", consumer=self.consumer_name)

    async def _maybe_claim(self) -> list[tuple[str, IngestMessage]]:
        """Periodically adopt entries abandoned by a dead consumer.

        Rate-limited because XAUTOCLAIM scans the pending entries list: running it on
        every read would add avoidable load to the hot path for a recovery case that
        only matters after a crash.
        """
        now = time.monotonic()
        if now - self._last_claim < self._settings.claim_interval_s:
            return []
        self._last_claim = now
        claimed = await self._stream.claim_stale(
            self.consumer_name,
            min_idle_ms=self._settings.claim_min_idle_ms,
            count=self._settings.stream_batch_size,
        )
        if claimed:
            log.info("worker.claimed_stale", count=len(claimed), consumer=self.consumer_name)
        return claimed

    async def _refresh_gauges(self) -> None:
        try:
            QUEUE_DEPTH.set(await self._stream.depth())
            QUEUE_PENDING.set(await self._stream.pending_count())
        except Exception:  # noqa: BLE001 -- gauges are never worth failing a batch over
            pass


__all__ = ["MatcherWorker", "ProcessOutcome", "MatchLayer", "ReconStatus"]

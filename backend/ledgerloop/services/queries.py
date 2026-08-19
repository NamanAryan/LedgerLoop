"""Read-path queries: stats, the transaction feed, and the exception queue.

Two rules hold throughout:

* Pagination is keyset, never OFFSET. ``OFFSET 100000`` makes PostgreSQL walk and
  discard 100000 rows on every page, so the last page of a feed costs the most --
  precisely when someone is scrolling a busy queue. ``WHERE id < :cursor ORDER BY
  id DESC LIMIT :n`` is an index descent, and costs the same on page 1 and page 1000.
* Aggregation happens in PostgreSQL, not in Python. Pulling a window of rows over the
  wire to count them in a loop would make /v1/stats scale with traffic instead of with
  the number of distinct answers it returns.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import Select, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from ledgerloop.db.enums import MatchLayer, ReconStatus
from ledgerloop.db.models import Exception_, ReconciliationResult

WindowName = Literal["1h", "24h", "7d"]

WINDOW_SECONDS: dict[str, int] = {"1h": 3600, "24h": 86_400, "7d": 604_800}

_UNMATCHED = (ReconStatus.UNMATCHED_GATEWAY_ONLY, ReconStatus.UNMATCHED_LEDGER_ONLY)
_DRIFT = (ReconStatus.AMOUNT_DRIFT, ReconStatus.TIME_DRIFT)


@dataclass(frozen=True, slots=True)
class StatsRow:
    matched: int
    unmatched: int
    unmatched_gateway: int
    unmatched_ledger: int
    via_time_drift: int
    duplicates: int
    drift: int
    p50: float | None
    p95: float | None
    p99: float | None
    open_exceptions: int


async def fetch_stats(session: AsyncSession, window: WindowName) -> StatsRow:
    """One pass over the window for every count and percentile.

    ``count(*) FILTER (WHERE ...)`` rather than five separate queries: the rows are
    read once, and the ix_reconciliation_results_resolved_at range scan is paid for
    once instead of five times.
    """
    seconds = WINDOW_SECONDS[window]
    cutoff = func.now() - text(f"interval '{seconds} seconds'")
    latency = ReconciliationResult.match_latency_ms
    # Percentiles deliberately exclude duplicates: a duplicate is resolved the instant
    # it is recognised, so including them would drag every percentile toward zero and
    # make the engine look faster than it reconciles.
    active = ReconciliationResult.status != ReconStatus.DUPLICATE

    stmt = select(
        func.count().filter(ReconciliationResult.status == ReconStatus.MATCHED).label("matched"),
        func.count().filter(ReconciliationResult.status.in_(_UNMATCHED)).label("unmatched"),
        # Split out because "46 unmatched" and "31 gateway-only, 15 ledger-only" are
        # different operational stories: the first says reconciliation is imperfect,
        # the second says which side stopped posting. Same scan, two more filters.
        func.count()
        .filter(ReconciliationResult.status == ReconStatus.UNMATCHED_GATEWAY_ONLY)
        .label("unmatched_gateway"),
        func.count()
        .filter(ReconciliationResult.status == ReconStatus.UNMATCHED_LEDGER_ONLY)
        .label("unmatched_ledger"),
        # Matched, but only after the fuzzy-time layer. Keyed on match_layer rather
        # than status precisely because these count as matches (see enums.py) -- this
        # is the only way to see how much of the match rate leans on clock tolerance.
        func.count()
        .filter(ReconciliationResult.match_layer == MatchLayer.TIME_DRIFT)
        .label("via_time_drift"),
        func.count().filter(ReconciliationResult.status == ReconStatus.DUPLICATE).label("dupes"),
        func.count().filter(ReconciliationResult.status.in_(_DRIFT)).label("drift"),
        func.percentile_cont(0.5).within_group(latency.asc()).filter(active).label("p50"),
        func.percentile_cont(0.95).within_group(latency.asc()).filter(active).label("p95"),
        func.percentile_cont(0.99).within_group(latency.asc()).filter(active).label("p99"),
    ).where(ReconciliationResult.resolved_at >= cutoff)

    row = (await session.execute(stmt)).one()

    # The open queue is not window-scoped: a break opened last week is still open work.
    open_count = (
        await session.execute(
            select(func.count()).select_from(Exception_).where(Exception_.closed_at.is_(None))
        )
    ).scalar_one()

    return StatsRow(
        matched=row.matched,
        unmatched=row.unmatched,
        unmatched_gateway=row.unmatched_gateway,
        unmatched_ledger=row.unmatched_ledger,
        via_time_drift=row.via_time_drift,
        duplicates=row.dupes,
        drift=row.drift,
        p50=float(row.p50) if row.p50 is not None else None,
        p95=float(row.p95) if row.p95 is not None else None,
        p99=float(row.p99) if row.p99 is not None else None,
        open_exceptions=open_count,
    )


def _apply_keyset(stmt: Select, column, cursor: int | None, limit: int) -> Select:  # type: ignore[type-arg]
    """Descending keyset page, fetching one extra row to detect "there is more"
    without a second COUNT query."""
    if cursor is not None:
        stmt = stmt.where(column < cursor)
    return stmt.order_by(column.desc()).limit(limit + 1)


async def fetch_results_page(
    session: AsyncSession,
    *,
    status: ReconStatus | None,
    limit: int,
    cursor: int | None,
) -> tuple[list[ReconciliationResult], int | None]:
    stmt = select(ReconciliationResult)
    if status is not None:
        # Hits ix_reconciliation_results_status_id, which already carries id DESC,
        # so the ORDER BY is free.
        stmt = stmt.where(ReconciliationResult.status == status)
    stmt = _apply_keyset(stmt, ReconciliationResult.id, cursor, limit)

    # Both sides come back with the page. The relationships are lazy="raise", so
    # without this the response serialiser would raise rather than silently emit N+1
    # queries -- the mapper is configured to make the accident impossible and the
    # intent explicit. LEFT OUTER (innerjoin=False) is required, not incidental: an
    # unmatched result has exactly one side by definition, and an inner join would
    # drop every break from the feed -- the rows an operator most needs to see.
    stmt = stmt.options(
        joinedload(ReconciliationResult.gateway_txn),
        joinedload(ReconciliationResult.ledger_entry),
    )

    rows = list((await session.execute(stmt)).unique().scalars().all())
    if len(rows) > limit:
        return rows[:limit], rows[limit - 1].id
    return rows, None


async def fetch_exceptions_page(
    session: AsyncSession,
    *,
    status: Literal["open", "closed"] | None,
    limit: int,
    cursor: int | None,
) -> tuple[list[tuple[Exception_, ReconciliationResult]], int | None]:
    """Exception queue joined to the result that opened it.

    The join is eager and explicit rather than a lazy relationship load: a lazy load
    would issue one query per row (the classic N+1), which on a 100-row page is 101
    round trips for data a single join already has.
    """
    stmt = select(Exception_, ReconciliationResult).join(
        ReconciliationResult, Exception_.reconciliation_result_id == ReconciliationResult.id
    )
    # The raw sides ride along with the same statement. An exception exists to be
    # judged by a human, and "result 41822 is in amount drift" is not something anyone
    # can judge -- they need the two amounts that disagree and the transaction id.
    stmt = stmt.options(
        joinedload(ReconciliationResult.gateway_txn),
        joinedload(ReconciliationResult.ledger_entry),
    )
    if status == "open":
        stmt = stmt.where(Exception_.closed_at.is_(None))  # -> ix_exceptions_open_id
    elif status == "closed":
        stmt = stmt.where(Exception_.closed_at.is_not(None))  # -> ix_exceptions_closed_id
    stmt = _apply_keyset(stmt, Exception_.id, cursor, limit)

    rows = [(row[0], row[1]) for row in (await session.execute(stmt)).unique().all()]
    if len(rows) > limit:
        return rows[:limit], rows[limit - 1][0].id
    return rows, None


async def fetch_exception(
    session: AsyncSession, exception_id: int
) -> tuple[Exception_, ReconciliationResult] | None:
    stmt = (
        select(Exception_, ReconciliationResult)
        .join(ReconciliationResult, Exception_.reconciliation_result_id == ReconciliationResult.id)
        .where(Exception_.id == exception_id)
        .options(
            joinedload(ReconciliationResult.gateway_txn),
            joinedload(ReconciliationResult.ledger_entry),
        )
    )
    row = (await session.execute(stmt)).unique().first()
    return (row[0], row[1]) if row else None


async def close_exception(
    session: AsyncSession, exception_id: int, notes: str
) -> Exception_ | None:
    """Close an open exception. Returns None if it was already closed or absent.

    The ``closed_at IS NULL`` predicate makes this a compare-and-set: two operators
    resolving the same break at the same moment cannot both win, and the loser is told
    so rather than silently overwriting the first resolution note.

    Isolation: READ COMMITTED. The conditional UPDATE is atomic on its own row, so no
    stronger level is needed -- the row lock the UPDATE takes serialises the racers.
    """
    stmt = (
        update(Exception_)
        .where(Exception_.id == exception_id, Exception_.closed_at.is_(None))
        .values(closed_at=func.now(), resolution_notes=notes)
        .returning(Exception_)
    )
    return (await session.execute(stmt)).scalars().one_or_none()

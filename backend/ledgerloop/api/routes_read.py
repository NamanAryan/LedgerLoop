"""Read path: stats, the reconciliation feed, and the exception queue."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query, status

from ledgerloop.api.deps import SessionDep
from ledgerloop.api.schemas import (
    ExceptionOut,
    ExceptionPage,
    ExceptionResolveIn,
    LatencyPercentiles,
    ReconciliationResultOut,
    StatsOut,
    TransactionPage,
)
from ledgerloop.db.enums import ReconStatus
from ledgerloop.services.queries import (
    WINDOW_SECONDS,
    close_exception,
    fetch_exception,
    fetch_exceptions_page,
    fetch_results_page,
    fetch_stats,
)

router = APIRouter(prefix="/v1", tags=["read"])

CursorQuery = Annotated[
    str | None,
    Query(description="Opaque cursor from the previous page's next_cursor. Not an offset."),
]
LimitQuery = Annotated[int, Query(ge=1, le=500)]


def _parse_cursor(cursor: str | None) -> int | None:
    """Cursors are opaque to clients but are ids underneath.

    Rejecting a malformed cursor with 400 beats silently falling back to page 1, which
    would make a client's pagination loop restart forever without ever erroring.
    """
    if cursor is None:
        return None
    try:
        return int(cursor)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid cursor") from None


@router.get("/stats", response_model=StatsOut, summary="Reconciliation health over a window")
async def get_stats(
    session: SessionDep,
    window: Annotated[Literal["1h", "24h", "7d"], Query()] = "1h",
) -> StatsOut:
    row = await fetch_stats(session, window)
    seconds = WINDOW_SECONDS[window]

    # Duplicates are excluded from the denominator. They are not reconciliation
    # failures -- they are the idempotency layer doing its job -- and counting them
    # would let a client depress its own match rate purely by retrying.
    total = row.matched + row.unmatched + row.drift
    return StatsOut(
        window=window,
        window_seconds=seconds,
        matched=row.matched,
        unmatched=row.unmatched,
        duplicates=row.duplicates,
        drift=row.drift,
        total=total,
        match_rate=(row.matched / total) if total else 0.0,
        latency_ms=LatencyPercentiles(p50=row.p50, p95=row.p95, p99=row.p99),
        throughput_tx_per_sec=total / seconds if seconds else 0.0,
        open_exceptions=row.open_exceptions,
    )


@router.get(
    "/transactions",
    response_model=TransactionPage,
    summary="Cursor-paginated feed of reconciliation results",
)
async def list_transactions(
    session: SessionDep,
    status_filter: Annotated[ReconStatus | None, Query(alias="status")] = None,
    limit: LimitQuery = 50,
    cursor: CursorQuery = None,
) -> TransactionPage:
    rows, next_cursor = await fetch_results_page(
        session, status=status_filter, limit=limit, cursor=_parse_cursor(cursor)
    )
    return TransactionPage(
        items=[
            ReconciliationResultOut(
                id=row.id,
                gateway_txn_id=row.gateway_txn_id,
                ledger_entry_id=row.ledger_entry_id,
                status=row.status,
                match_layer=row.match_layer,
                resolved_at=row.resolved_at,
                match_latency_ms=row.match_latency_ms,
                notes=row.notes,
            )
            for row in rows
        ],
        next_cursor=str(next_cursor) if next_cursor is not None else None,
    )


def _exception_out(exc, result) -> ExceptionOut:  # type: ignore[no-untyped-def]
    return ExceptionOut(
        id=exc.id,
        reconciliation_result_id=exc.reconciliation_result_id,
        opened_at=exc.opened_at,
        closed_at=exc.closed_at,
        resolution_notes=exc.resolution_notes,
        status=result.status,
        match_layer=result.match_layer,
        gateway_txn_id=result.gateway_txn_id,
        ledger_entry_id=result.ledger_entry_id,
        notes=result.notes,
    )


@router.get("/exceptions", response_model=ExceptionPage, summary="Exception queue")
async def list_exceptions(
    session: SessionDep,
    status_filter: Annotated[Literal["open", "closed"] | None, Query(alias="status")] = None,
    limit: LimitQuery = 50,
    cursor: CursorQuery = None,
) -> ExceptionPage:
    rows, next_cursor = await fetch_exceptions_page(
        session, status=status_filter, limit=limit, cursor=_parse_cursor(cursor)
    )
    return ExceptionPage(
        items=[_exception_out(exc, result) for exc, result in rows],
        next_cursor=str(next_cursor) if next_cursor is not None else None,
    )


@router.post(
    "/exceptions/{exception_id}/resolve",
    response_model=ExceptionOut,
    summary="Close an exception with resolution notes",
)
async def resolve_exception(
    session: SessionDep,
    payload: ExceptionResolveIn,
    exception_id: Annotated[int, Path(ge=1)],
) -> ExceptionOut:
    # Isolation: READ COMMITTED (PostgreSQL default). The conditional UPDATE inside
    # close_exception() is a compare-and-set on one row, and the row lock it takes is
    # what serialises two operators resolving the same break.
    async with session.begin():
        closed = await close_exception(session, exception_id, payload.resolution_notes)
        if closed is None:
            existing = await fetch_exception(session, exception_id)
            if existing is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
            # Already closed. 409, not 200: the caller's notes were not applied, and
            # telling them otherwise would lose a human's actual resolution.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="exception already closed"
            )

    found = await fetch_exception(session, exception_id)
    assert found is not None  # just closed it inside this request
    return _exception_out(*found)

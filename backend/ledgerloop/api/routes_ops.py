"""Liveness, readiness, and metrics.

/health and /ready are deliberately different questions. Liveness asks "is this
process wedged?" -- if it answers no, the right response is to restart the container.
Readiness asks "can this process serve traffic right now?" -- if it answers no, the
right response is to take it out of the load balancer and leave it alone. Wiring a
dependency check into the liveness probe is how a brief database blip turns into an
orchestrator restart-looping every replica at once.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import func, select, text

from ledgerloop.api.deps import SessionDep, SettingsDep, StreamDep
from ledgerloop.api.schemas import DependencyStatus, HealthOut, ReadyOut
from ledgerloop.db.models import OutboxEvent
from ledgerloop.observability.metrics import OUTBOX_BACKLOG, QUEUE_DEPTH, QUEUE_PENDING, REGISTRY

router = APIRouter(tags=["ops"])


@router.get("/health", response_model=HealthOut, summary="Liveness")
async def health(settings: SettingsDep) -> HealthOut:
    # No dependency checks on purpose. If this process can execute a handler, it is
    # alive; whether Postgres is reachable is /ready's question.
    return HealthOut(service=settings.service_name)


@router.get("/ready", response_model=ReadyOut, summary="Readiness")
async def ready(session: SessionDep, stream: StreamDep, response: Response) -> ReadyOut:
    db = DependencyStatus(ok=True)
    try:
        await session.execute(select(text("1")))
    except Exception as exc:  # noqa: BLE001 -- readiness reports, it does not handle
        db = DependencyStatus(ok=False, detail=f"{type(exc).__name__}: {exc}")

    redis_status = DependencyStatus(ok=True)
    try:
        await stream.redis.ping()
    except Exception as exc:  # noqa: BLE001
        redis_status = DependencyStatus(ok=False, detail=f"{type(exc).__name__}: {exc}")

    is_ready = db.ok and redis_status.ok
    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyOut(ready=is_ready, database=db, redis=redis_status)


@router.get("/metrics", summary="Prometheus exposition")
async def metrics(session: SessionDep, stream: StreamDep) -> Response:
    """Counters and histograms accumulate as work happens; the three gauges below are
    point-in-time facts about queues, so they are sampled here at scrape time rather
    than maintained on the hot path."""
    try:
        QUEUE_DEPTH.set(await stream.depth())
        QUEUE_PENDING.set(await stream.pending_count())
    except Exception:  # noqa: BLE001 -- a scrape must never fail because Redis blipped
        pass
    try:
        backlog = (
            await session.execute(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.published_at.is_(None))
            )
        ).scalar_one()
        OUTBOX_BACKLOG.set(backlog)
    except Exception:  # noqa: BLE001
        pass

    return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

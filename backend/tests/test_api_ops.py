"""Liveness, readiness, metrics, and the OpenAPI contract."""

from __future__ import annotations

import httpx
import pytest

from ledgerloop.api.app import create_app
from tests.helpers import ingest_pair
from tests.pipeline import run_pipeline


async def test_health_is_up(api):
    response = await api.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_ready_reports_both_dependencies(api):
    response = await api.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["database"]["ok"] is True
    assert body["redis"]["ok"] is True


async def test_ready_is_503_when_redis_is_unreachable(settings):
    """Readiness must fail closed. Pointing at a dead port is the honest simulation of
    Redis being down; the process is still alive, it just cannot serve."""
    broken = settings.model_copy(update={"redis_url": "redis://127.0.0.1:6399/0"})
    app = create_app(broken)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/ready")
            assert response.status_code == 503
            body = response.json()
            assert body["ready"] is False
            assert body["redis"]["ok"] is False
            assert body["database"]["ok"] is True


async def test_health_stays_up_even_when_redis_is_down(settings):
    """The distinction that matters: liveness must not depend on Redis, or a brief blip
    restart-loops every replica at once."""
    broken = settings.model_copy(update={"redis_url": "redis://127.0.0.1:6399/0"})
    app = create_app(broken)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/health")).status_code == 200


async def test_metrics_are_prometheus_formatted(api):
    response = await api.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    body = response.text
    assert "# HELP" in body
    assert "# TYPE" in body


@pytest.mark.parametrize(
    "metric",
    [
        "ledgerloop_ingest_total",
        "ledgerloop_queue_depth",
        "ledgerloop_outbox_backlog",
        "ledgerloop_matcher_duration_seconds",
        "ledgerloop_errors_total",
    ],
)
async def test_required_metrics_are_exposed(api, sessions, stream, settings, metric):
    await ingest_pair(api, "TXN-METRIC")
    await run_pipeline(sessions, stream, settings)
    assert metric in (await api.get("/metrics")).text


async def test_ingest_counter_moves(api):
    before = (await api.get("/metrics")).text
    await ingest_pair(api, "TXN-COUNTER")
    after = (await api.get("/metrics")).text
    assert before != after
    assert 'endpoint="gateway_webhook"' in after
    assert 'endpoint="ledger_sync"' in after


async def test_queue_depth_gauge_reflects_the_stream(api, sessions, stream, settings):
    from tests.pipeline import publish_all

    await ingest_pair(api, "TXN-DEPTH")
    await publish_all(sessions, stream, settings)

    body = (await api.get("/metrics")).text
    depth_line = next(
        line
        for line in body.splitlines()
        if line.startswith("ledgerloop_queue_depth") and not line.startswith("#")
    )
    assert float(depth_line.split()[-1]) == 2.0


async def test_openapi_document_is_generated(api):
    document = (await api.get("/openapi.json")).json()
    paths = document["paths"]
    for path in (
        "/v1/gateway/webhook",
        "/v1/ledger/sync",
        "/v1/stats",
        "/v1/transactions",
        "/v1/exceptions",
        "/v1/exceptions/{exception_id}/resolve",
        "/health",
        "/ready",
        "/metrics",
    ):
        assert path in paths, f"{path} missing from the OpenAPI document"


async def test_endpoints_declare_response_models(api):
    """No dict[str, Any] in a signature means every endpoint has a real schema."""
    document = (await api.get("/openapi.json")).json()
    webhook = document["paths"]["/v1/gateway/webhook"]["post"]
    assert "202" in webhook["responses"]
    schema = webhook["responses"]["202"]["content"]["application/json"]["schema"]
    assert "$ref" in schema or "properties" in schema

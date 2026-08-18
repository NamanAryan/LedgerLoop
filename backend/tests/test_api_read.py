"""Read path: /v1/stats, /v1/transactions, /v1/exceptions, and exception resolution."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, update

from ledgerloop.db.models import GatewayTransaction, LedgerEntry
from ledgerloop.worker.sweeper import Sweeper
from tests.helpers import gateway_payload, ingest_pair, post_gateway
from tests.pipeline import run_pipeline


async def _age_rows(session, seconds: int = 10) -> None:  # type: ignore[no-untyped-def]
    for model in (GatewayTransaction, LedgerEntry):
        await session.execute(
            update(model).values(
                received_at=func.now() - func.make_interval(0, 0, 0, 0, 0, 0, seconds)
            )
        )
    await session.commit()


# --- stats -----------------------------------------------------------------
async def test_stats_on_an_empty_engine(api):
    body = (await api.get("/v1/stats")).json()
    assert body["matched"] == 0
    assert body["total"] == 0
    assert body["match_rate"] == 0.0  # not a division by zero
    assert body["latency_ms"] == {"p50": None, "p95": None, "p99": None}


async def test_stats_counts_matches(api, sessions, stream, settings):
    for index in range(5):
        await ingest_pair(api, f"TXN-ST-{index}")
    await run_pipeline(sessions, stream, settings)

    body = (await api.get("/v1/stats")).json()
    assert body["matched"] == 5
    assert body["total"] == 5
    assert body["match_rate"] == 1.0


async def test_stats_reports_latency_percentiles(api, sessions, stream, settings):
    for index in range(5):
        await ingest_pair(api, f"TXN-LAT-{index}")
    await run_pipeline(sessions, stream, settings)

    latency = (await api.get("/v1/stats")).json()["latency_ms"]
    assert latency["p50"] is not None
    assert latency["p99"] >= latency["p50"]


async def test_stats_counts_drift_separately_from_matches(api, sessions, stream, settings):
    await ingest_pair(api, "TXN-OK")
    await ingest_pair(api, "TXN-DR", gateway_amount="1000.00", ledger_amount="1005.00")
    await run_pipeline(sessions, stream, settings)

    body = (await api.get("/v1/stats")).json()
    assert body["matched"] == 1
    assert body["drift"] == 1
    assert body["total"] == 2
    assert body["match_rate"] == 0.5


async def test_duplicates_are_excluded_from_the_match_rate(api, sessions, stream, settings):
    """A client that retries must not be able to depress its own match rate. Duplicates
    are the idempotency layer working, not reconciliation failures."""
    await ingest_pair(api, "TXN-DUPRATE")
    await post_gateway(api, gateway_payload("TXN-DUPRATE"))  # same key -> retry
    await run_pipeline(sessions, stream, settings)

    body = (await api.get("/v1/stats")).json()
    assert body["duplicates"] == 1
    assert body["total"] == 1
    assert body["match_rate"] == 1.0


async def test_stats_counts_unmatched_and_open_exceptions(api, sessions, settings, session):
    await post_gateway(api, gateway_payload("TXN-UM"))
    await _age_rows(session)
    await Sweeper(sessions, settings).sweep_once()

    body = (await api.get("/v1/stats")).json()
    assert body["unmatched"] == 1
    assert body["open_exceptions"] == 1
    assert body["match_rate"] == 0.0


@pytest.mark.parametrize(("window", "seconds"), [("1h", 3600), ("24h", 86400), ("7d", 604800)])
async def test_stats_windows(api, window, seconds):
    body = (await api.get("/v1/stats", params={"window": window})).json()
    assert body["window"] == window
    assert body["window_seconds"] == seconds


async def test_unknown_window_is_rejected(api):
    assert (await api.get("/v1/stats", params={"window": "30d"})).status_code == 422


async def test_throughput_is_derived_from_the_window(api, sessions, stream, settings):
    for index in range(10):
        await ingest_pair(api, f"TXN-TP-{index}")
    await run_pipeline(sessions, stream, settings)

    body = (await api.get("/v1/stats", params={"window": "1h"})).json()
    assert body["throughput_tx_per_sec"] == pytest.approx(10 / 3600)


# --- transactions feed -----------------------------------------------------
async def test_transactions_feed_returns_results(api, sessions, stream, settings):
    for index in range(3):
        await ingest_pair(api, f"TXN-F-{index}")
    await run_pipeline(sessions, stream, settings)

    body = (await api.get("/v1/transactions")).json()
    assert len(body["items"]) == 3
    assert body["next_cursor"] is None
    assert body["items"][0]["status"] == "matched"
    assert body["items"][0]["match_layer"] == "exact"


async def test_transactions_feed_is_newest_first(api, sessions, stream, settings):
    for index in range(5):
        await ingest_pair(api, f"TXN-ORD-{index}")
        await run_pipeline(sessions, stream, settings)

    ids = [item["id"] for item in (await api.get("/v1/transactions")).json()["items"]]
    assert ids == sorted(ids, reverse=True)


async def test_keyset_pagination_walks_the_whole_feed_without_overlap(
    api, sessions, stream, settings
):
    for index in range(10):
        await ingest_pair(api, f"TXN-PG-{index}")
    await run_pipeline(sessions, stream, settings)

    seen: list[int] = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 3}
        if cursor:
            params["cursor"] = cursor
        body = (await api.get("/v1/transactions", params=params)).json()
        seen.extend(item["id"] for item in body["items"])
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
        assert pages < 20, "pagination did not terminate"

    assert len(seen) == 10
    assert len(set(seen)) == 10  # no row served twice, none skipped


async def test_status_filter(api, sessions, stream, settings):
    await ingest_pair(api, "TXN-FLT-OK")
    await ingest_pair(api, "TXN-FLT-DR", gateway_amount="1000.00", ledger_amount="1005.00")
    await run_pipeline(sessions, stream, settings)

    body = (await api.get("/v1/transactions", params={"status": "amount_drift"})).json()
    assert len(body["items"]) == 1
    assert body["items"][0]["status"] == "amount_drift"


async def test_unknown_status_is_rejected(api):
    assert (await api.get("/v1/transactions", params={"status": "not_a_status"})).status_code == 422


async def test_malformed_cursor_is_rejected(api):
    """400 rather than silently restarting at page 1, which would make a client's
    pagination loop spin forever without ever erroring."""
    response = await api.get("/v1/transactions", params={"cursor": "abc"})
    assert response.status_code == 400


@pytest.mark.parametrize("limit", [0, 501, -1])
async def test_limit_bounds_are_enforced(api, limit):
    assert (await api.get("/v1/transactions", params={"limit": limit})).status_code == 422


# --- exception queue -------------------------------------------------------
async def test_exception_queue_lists_open_items(api, sessions, stream, settings):
    await ingest_pair(api, "TXN-EX1", gateway_amount="1000.00", ledger_amount="1005.00")
    await run_pipeline(sessions, stream, settings)

    body = (await api.get("/v1/exceptions", params={"status": "open"})).json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["closed_at"] is None
    assert item["status"] == "amount_drift"
    assert item["notes"] is not None  # the drift explanation travels with the case


async def test_resolving_an_exception_closes_it(api, sessions, stream, settings):
    await ingest_pair(api, "TXN-EX2", gateway_amount="1000.00", ledger_amount="1005.00")
    await run_pipeline(sessions, stream, settings)
    exception_id = (await api.get("/v1/exceptions")).json()["items"][0]["id"]

    response = await api.post(
        f"/v1/exceptions/{exception_id}/resolve",
        json={"resolution_notes": "FX rounding, accepted by ops"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["closed_at"] is not None
    assert body["resolution_notes"] == "FX rounding, accepted by ops"


async def test_closed_exceptions_leave_the_open_queue(api, sessions, stream, settings):
    await ingest_pair(api, "TXN-EX3", gateway_amount="1000.00", ledger_amount="1005.00")
    await run_pipeline(sessions, stream, settings)
    exception_id = (await api.get("/v1/exceptions")).json()["items"][0]["id"]
    await api.post(f"/v1/exceptions/{exception_id}/resolve", json={"resolution_notes": "done"})

    assert (await api.get("/v1/exceptions", params={"status": "open"})).json()["items"] == []
    closed = (await api.get("/v1/exceptions", params={"status": "closed"})).json()["items"]
    assert len(closed) == 1


async def test_resolving_twice_is_a_conflict(api, sessions, stream, settings):
    """Compare-and-set: the second operator is told their notes were not applied,
    rather than silently overwriting the first resolution."""
    await ingest_pair(api, "TXN-EX4", gateway_amount="1000.00", ledger_amount="1005.00")
    await run_pipeline(sessions, stream, settings)
    exception_id = (await api.get("/v1/exceptions")).json()["items"][0]["id"]

    await api.post(f"/v1/exceptions/{exception_id}/resolve", json={"resolution_notes": "first"})
    second = await api.post(
        f"/v1/exceptions/{exception_id}/resolve", json={"resolution_notes": "second"}
    )
    assert second.status_code == 409

    body = (await api.get("/v1/exceptions", params={"status": "closed"})).json()["items"][0]
    assert body["resolution_notes"] == "first"


async def test_resolving_a_missing_exception_is_404(api):
    response = await api.post("/v1/exceptions/424242/resolve", json={"resolution_notes": "x"})
    assert response.status_code == 404


async def test_resolution_notes_are_required(api):
    assert (
        await api.post("/v1/exceptions/1/resolve", json={"resolution_notes": ""})
    ).status_code == 422
    assert (await api.post("/v1/exceptions/1/resolve", json={})).status_code == 422


async def test_exception_pagination(api, sessions, stream, settings):
    for index in range(6):
        await ingest_pair(
            api, f"TXN-EXP-{index}", gateway_amount="1000.00", ledger_amount="1005.00"
        )
    await run_pipeline(sessions, stream, settings)

    first = (await api.get("/v1/exceptions", params={"limit": 4})).json()
    assert len(first["items"]) == 4
    assert first["next_cursor"] is not None

    second = (
        await api.get("/v1/exceptions", params={"limit": 4, "cursor": first["next_cursor"]})
    ).json()
    assert len(second["items"]) == 2
    assert second["next_cursor"] is None

    ids = [item["id"] for item in first["items"] + second["items"]]
    assert len(set(ids)) == 6


async def test_unmatched_sweep_shows_up_in_the_exception_queue(api, sessions, settings, session):
    await post_gateway(api, gateway_payload("TXN-EXSW"))
    await _age_rows(session)
    await Sweeper(sessions, settings).sweep_once()

    items = (await api.get("/v1/exceptions", params={"status": "open"})).json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "unmatched_gateway_only"
    assert items[0]["match_layer"] == "unmatched_sweep"


async def test_time_drift_does_not_create_an_exception(api, sessions, stream, settings):
    await ingest_pair(api, "TXN-NOEX", skew=timedelta(seconds=30))
    await run_pipeline(sessions, stream, settings)
    assert (await api.get("/v1/exceptions")).json()["items"] == []


# --- feed enrichment (dashboard rendering) ---------------------------------
async def test_feed_carries_both_amounts_and_the_business_id(api, sessions, stream, settings):
    """The dashboard renders a drift break without a second request per row."""
    await ingest_pair(api, "TXN-ENR", gateway_amount="1000.00", ledger_amount="1005.00")
    await run_pipeline(sessions, stream, settings)

    item = (await api.get("/v1/transactions")).json()["items"][0]
    assert item["txn_id"] == "TXN-ENR"
    assert item["currency"] == "INR"
    assert item["gateway_amount"] == "1000.00"
    assert item["ledger_amount"] == "1005.00"


async def test_feed_matched_row_reports_equal_amounts(api, sessions, stream, settings):
    await ingest_pair(api, "TXN-EQ", gateway_amount="250.00")
    await run_pipeline(sessions, stream, settings)

    item = (await api.get("/v1/transactions")).json()["items"][0]
    assert item["gateway_amount"] == item["ledger_amount"] == "250.00"


async def test_feed_unmatched_row_has_one_side_null(api, sessions, settings, session):
    """An unmatched break has no counterparty; the missing side must serialise as null
    rather than dropping the row from the feed entirely."""
    await post_gateway(api, gateway_payload("TXN-ONLY"))
    await _age_rows(session)
    await Sweeper(sessions, settings).sweep_once()

    items = (await api.get("/v1/transactions")).json()["items"]
    row = next(item for item in items if item["txn_id"] == "TXN-ONLY")
    assert row["status"] == "unmatched_gateway_only"
    assert row["gateway_amount"] == "1000.00"
    assert row["ledger_amount"] is None


async def test_feed_page_costs_one_query_regardless_of_size(api, sessions, stream, settings):
    """Guards the N+1 the joinedload exists to prevent: lazy="raise" on both
    relationships turns an accidental lazy load into an error, not a slow page."""
    for index in range(10):
        await ingest_pair(api, f"TXN-N1-{index}")
    await run_pipeline(sessions, stream, settings)

    response = await api.get("/v1/transactions", params={"limit": 10})
    assert response.status_code == 200
    assert len(response.json()["items"]) == 10


# --- CORS -------------------------------------------------------------------
async def test_cors_allows_the_dashboard_origin(api):
    response = await api.get("/v1/stats", headers={"Origin": "http://localhost:5173"})
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


async def test_cors_omits_headers_for_an_unlisted_origin(api):
    response = await api.get("/v1/stats", headers={"Origin": "http://evil.example"})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers

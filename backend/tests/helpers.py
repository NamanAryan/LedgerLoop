"""Helpers for tests that go through the HTTP surface."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx


def now() -> datetime:
    return datetime.now(UTC)


def gateway_payload(
    txn_id: str = "TXN-1",
    amount: str = "1000.00",
    currency: str = "INR",
    occurred_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "txn_id": txn_id,
        "amount": amount,
        "currency": currency,
        "occurred_at": (occurred_at or now()).isoformat(),
        "gateway_ref": f"REF-{txn_id}",
    }


def ledger_payload(
    txn_id: str = "TXN-1",
    amount: str = "1000.00",
    currency: str = "INR",
    occurred_at: datetime | None = None,
    idempotency_key: str | None = None,
    entry_id: str | None = None,
) -> dict[str, Any]:
    return {
        "entry_id": entry_id or f"LEDG-{txn_id}",
        "txn_id": txn_id,
        "amount": amount,
        "currency": currency,
        "occurred_at": (occurred_at or now()).isoformat(),
        "idempotency_key": idempotency_key or f"ld-{txn_id}",
    }


async def post_gateway(
    api: httpx.AsyncClient, payload: dict[str, Any], key: str | None = None
) -> httpx.Response:
    return await api.post(
        "/v1/gateway/webhook",
        json=payload,
        headers={"Idempotency-Key": key or f"gw-{payload['txn_id']}"},
    )


async def post_ledger(api: httpx.AsyncClient, *entries: dict[str, Any]) -> httpx.Response:
    return await api.post("/v1/ledger/sync", json={"entries": list(entries)})


async def ingest_pair(
    api: httpx.AsyncClient,
    txn_id: str = "TXN-1",
    *,
    gateway_amount: str = "1000.00",
    ledger_amount: str | None = None,
    skew: timedelta = timedelta(0),
) -> datetime:
    """Post both sides of one transaction. Returns the shared occurrence instant."""
    occurred = now()
    await post_gateway(api, gateway_payload(txn_id, gateway_amount, occurred_at=occurred))
    await post_ledger(
        api,
        ledger_payload(txn_id, ledger_amount or gateway_amount, occurred_at=occurred + skew),
    )
    return occurred

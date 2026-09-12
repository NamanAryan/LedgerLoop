"""Multi-tenancy: the claims the tenant column makes, against real Postgres.

Every test here would pass trivially against a single-tenant schema *except* the ones
that matter, and those are the ones that would have caught the mistake this layer is
easiest to get wrong: leaving ``idempotency_key`` globally unique. That failure is
silent -- the second tenant gets a ``202`` with ``duplicate: true`` and no row -- so
the assertion has to be on the stored data, not on the status code.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ledgerloop.config import Settings
from ledgerloop.db.enums import AccountKind, IngestSource
from ledgerloop.db.models import Account, GatewayTransaction, LedgerEntry
from ledgerloop.matching.core import classify_pair
from ledgerloop.services.tenancy import (
    DEFAULT_ACCOUNT_NAME,
    hash_key,
    issue_api_key,
)
from ledgerloop.worker.sweeper import Sweeper
from tests.factories import DEFAULT_CONFIG, gateway, ledger
from tests.helpers import (
    demo_headers,
    gateway_payload,
    key_headers,
    ledger_payload,
    now,
    post_gateway,
    post_ledger,
)
from tests.pipeline import run_pipeline


def session_id() -> str:
    return str(uuid.uuid4())


async def mint_key(sessions: async_sessionmaker[AsyncSession], account: str) -> str:
    async with sessions() as session, session.begin():
        raw, _ = await issue_api_key(session, account, label="test")
    return raw


# --------------------------------------------------------------------------- #
# The collision the tenant column exists to prevent                             #
# --------------------------------------------------------------------------- #


async def test_same_idempotency_key_across_tenants_stores_both_rows(api, session) -> None:
    """The one that fails silently if the unique index was not rebuilt.

    Two tenants, the same idempotency key, the same txn_id. Before tenancy this was
    one row and the second caller was told ``duplicate: true``. The status code is
    ``202`` either way, which is exactly why this asserts on the stored rows.
    """
    a, b = session_id(), session_id()
    payload = gateway_payload("TXN-SHARED")

    first = await post_gateway(api, payload, key="same-key", headers=demo_headers(a))
    second = await post_gateway(api, payload, key="same-key", headers=demo_headers(b))

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["result"]["duplicate"] is False
    assert second.json()["result"]["duplicate"] is False, (
        "second tenant's genuine payment was swallowed as a duplicate -- "
        "uq_gateway_transactions_tenant_idempotency_key is not tenant-leading"
    )
    assert first.json()["result"]["row_id"] != second.json()["result"]["row_id"]

    stored = (
        await session.execute(
            select(func.count())
            .select_from(GatewayTransaction)
            .where(GatewayTransaction.idempotency_key == "same-key")
        )
    ).scalar_one()
    assert stored == 2

    tenants = (
        await session.execute(
            select(func.count(func.distinct(GatewayTransaction.tenant_id))).where(
                GatewayTransaction.idempotency_key == "same-key"
            )
        )
    ).scalar_one()
    assert tenants == 2


async def test_same_idempotency_key_within_one_tenant_is_still_a_duplicate(api) -> None:
    """The original guarantee survives: tenancy widened the key, it did not remove it."""
    a = session_id()
    payload = gateway_payload("TXN-RETRY")

    first = await post_gateway(api, payload, key="retry-key", headers=demo_headers(a))
    second = await post_gateway(api, payload, key="retry-key", headers=demo_headers(a))

    assert second.status_code == 202
    assert second.json()["result"]["duplicate"] is True
    assert second.json()["result"]["row_id"] == first.json()["result"]["row_id"]
    assert second.json()["result"]["submissions"] == 2


async def test_ledger_batch_key_collides_only_within_a_tenant(api, session) -> None:
    a, b = session_id(), session_id()
    entry = ledger_payload("TXN-LEDGER", idempotency_key="shared-ledger-key")

    first = await post_ledger(api, entry, headers=demo_headers(a))
    second = await post_ledger(api, entry, headers=demo_headers(b))

    assert first.json()["accepted"] == 1
    assert second.json()["accepted"] == 1, "ledger unique index is not tenant-leading"

    stored = (
        await session.execute(
            select(func.count())
            .select_from(LedgerEntry)
            .where(LedgerEntry.idempotency_key == "shared-ledger-key")
        )
    ).scalar_one()
    assert stored == 2


# --------------------------------------------------------------------------- #
# Read isolation                                                                #
# --------------------------------------------------------------------------- #


async def test_two_demo_sessions_cannot_see_each_other(api) -> None:
    a, b = session_id(), session_id()

    occurred = now()
    await post_gateway(
        api, gateway_payload("TXN-A", occurred_at=occurred), headers=demo_headers(a)
    )
    await post_ledger(
        api, ledger_payload("TXN-A", occurred_at=occurred), headers=demo_headers(a)
    )

    from_a = await api.get("/v1/transactions", headers=demo_headers(a))
    from_b = await api.get("/v1/transactions", headers=demo_headers(b))
    assert from_b.status_code == 200
    assert from_b.json()["items"] == []
    # A's own rows may still be mid-match; what matters is that B sees none of them,
    # and that B's empty feed is not an error.
    assert isinstance(from_a.json()["items"], list)


async def test_stats_are_scoped_to_the_caller(api, sessions, stream, settings) -> None:
    """A full ingest-match-read round trip on one tenant leaves another's stats at zero."""
    a, b = session_id(), session_id()
    occurred = now()
    await post_gateway(
        api, gateway_payload("TXN-S", occurred_at=occurred), headers=demo_headers(a)
    )
    await post_ledger(api, ledger_payload("TXN-S", occurred_at=occurred), headers=demo_headers(a))

    await run_pipeline(sessions, stream, settings)

    stats_a = (await api.get("/v1/stats", headers=demo_headers(a))).json()
    stats_b = (await api.get("/v1/stats", headers=demo_headers(b))).json()

    assert stats_a["matched"] == 1
    assert stats_b["matched"] == 0
    assert stats_b["total"] == 0
    assert stats_b["open_exceptions"] == 0


async def test_another_tenants_exception_id_is_a_404(api, sessions, stream, settings) -> None:
    """Not a 403. A 403 would confirm the id exists, which is a fact about other data."""
    a, b = session_id(), session_id()
    occurred = now()
    # Amount drift opens an exception on ingest+match, with no sweeper wait.
    await post_gateway(
        api,
        gateway_payload("TXN-D", amount="1000.00", occurred_at=occurred),
        headers=demo_headers(a),
    )
    await post_ledger(
        api,
        ledger_payload("TXN-D", amount="1005.00", occurred_at=occurred),
        headers=demo_headers(a),
    )
    await run_pipeline(sessions, stream, settings)

    mine = (await api.get("/v1/exceptions", headers=demo_headers(a))).json()["items"]
    assert mine, "expected an amount-drift exception for tenant A"
    exception_id = mine[0]["id"]

    assert (await api.get("/v1/exceptions", headers=demo_headers(b))).json()["items"] == []

    stolen = await api.post(
        f"/v1/exceptions/{exception_id}/resolve",
        json={"resolution_notes": "not mine"},
        headers=demo_headers(b),
    )
    assert stolen.status_code == 404

    ours = await api.post(
        f"/v1/exceptions/{exception_id}/resolve",
        json={"resolution_notes": "mine"},
        headers=demo_headers(a),
    )
    assert ours.status_code == 200


# --------------------------------------------------------------------------- #
# Matching never crosses the boundary                                           #
# --------------------------------------------------------------------------- #


def test_classify_pair_refuses_a_cross_tenant_pair() -> None:
    """Pure-layer guard: identical in every matchable respect except the tenant."""
    assert classify_pair(gateway(tenant_id=1), ledger(tenant_id=1), DEFAULT_CONFIG) is not None
    assert classify_pair(gateway(tenant_id=1), ledger(tenant_id=2), DEFAULT_CONFIG) is None


async def test_matcher_does_not_pair_across_tenants(api, sessions, stream, settings) -> None:
    """The same assertion end to end: A's gateway row and B's ledger row share a
    txn_id, an amount and an instant, and still resolve as two separate breaks."""
    a, b = session_id(), session_id()
    occurred = now()
    await post_gateway(
        api, gateway_payload("TXN-X", occurred_at=occurred), headers=demo_headers(a)
    )
    await post_ledger(api, ledger_payload("TXN-X", occurred_at=occurred), headers=demo_headers(b))

    await run_pipeline(sessions, stream, settings)

    for who in (a, b):
        stats = (await api.get("/v1/stats", headers=demo_headers(who))).json()
        assert stats["matched"] == 0, "a cross-tenant pair was matched"

    # Both rows are still pending, which is correct: neither has a counterparty, and
    # it is the sweeper's job -- not the matcher's -- to eventually call them breaks.
    async with sessions() as session:
        pending = (
            await session.execute(
                select(func.count())
                .select_from(GatewayTransaction)
                .where(GatewayTransaction.reconciled_at.is_(None))
            )
        ).scalar_one()
    assert pending == 1


# --------------------------------------------------------------------------- #
# Authentication                                                                #
# --------------------------------------------------------------------------- #


async def test_valid_key_resolves_to_its_own_account(api, sessions) -> None:
    raw = await mint_key(sessions, "acme")
    response = await post_gateway(api, gateway_payload("TXN-K"), headers=key_headers(raw))
    assert response.status_code == 202

    anonymous = await api.get("/v1/transactions")
    keyed = await api.get("/v1/transactions", headers=key_headers(raw))
    assert anonymous.status_code == 200
    assert keyed.status_code == 200
    # The keyed account's row is not visible on the shared default demo tenant.
    assert anonymous.json()["items"] == []


@pytest.mark.parametrize(
    "header",
    [
        {"Authorization": "Bearer llk_definitely-not-a-real-key"},
        {"Authorization": "Bearer "},
        {"Authorization": "Basic llk_whatever"},
        {"Authorization": "llk_no-scheme"},
    ],
)
async def test_bad_key_is_401_and_never_a_demo_fallback(api, header) -> None:
    """The important half is the *status code*, not the message.

    A fall-through to the demo tenant would return 200 with an empty-but-plausible
    dashboard, and a broken integration that renders fine is one nobody investigates.
    """
    response = await api.get("/v1/stats", headers=header)
    assert response.status_code == 401

    write = await post_gateway(api, gateway_payload("TXN-401"), headers=header)
    assert write.status_code == 401


async def test_revoked_key_is_401(api, sessions) -> None:
    raw = await mint_key(sessions, "revoked-co")
    assert (await api.get("/v1/stats", headers=key_headers(raw))).status_code == 200

    async with sessions() as session, session.begin():
        from ledgerloop.db.models import ApiKey

        await session.execute(
            update(ApiKey)
            .where(ApiKey.key_hash == hash_key(raw))
            .values(revoked_at=datetime.now(UTC))
        )

    assert (await api.get("/v1/stats", headers=key_headers(raw))).status_code == 401


async def test_raw_key_is_never_stored(sessions) -> None:
    raw = await mint_key(sessions, "storage-co")
    async with sessions() as session:
        from ledgerloop.db.models import ApiKey

        row = (
            await session.execute(select(ApiKey).where(ApiKey.key_hash == hash_key(raw)))
        ).scalar_one()
    assert row.key_hash != raw
    assert len(row.key_hash) == 64
    assert raw.startswith(row.prefix)
    assert len(row.prefix) < len(raw)


async def test_malformed_demo_session_is_400_not_a_shared_bucket(api) -> None:
    response = await api.get("/v1/stats", headers=demo_headers("not-a-uuid"))
    assert response.status_code == 400


async def test_no_headers_lands_on_the_shared_default_tenant(api, session) -> None:
    """What keeps scripts/generate_load and scripts/benchmark working unchanged."""
    first = await post_gateway(api, gateway_payload("TXN-ANON-1"))
    second = await post_gateway(api, gateway_payload("TXN-ANON-2"))
    assert first.status_code == 202
    assert second.status_code == 202

    account = (
        await session.execute(select(Account).where(Account.name == DEFAULT_ACCOUNT_NAME))
    ).scalar_one()
    assert account.kind is AccountKind.DEMO

    rows = (
        await session.execute(
            select(func.count())
            .select_from(GatewayTransaction)
            .where(GatewayTransaction.tenant_id == account.id)
        )
    ).scalar_one()
    assert rows == 2

    feed = await api.get("/v1/transactions")
    assert feed.status_code == 200


# --------------------------------------------------------------------------- #
# Rate limiting                                                                 #
# --------------------------------------------------------------------------- #


async def test_unauthenticated_writes_are_rate_limited(settings, redis_client) -> None:
    """Limit set to 2/min so the test is three requests, not sixty thousand."""
    import httpx

    from ledgerloop.api.app import create_app

    limited = settings.model_copy(update={"rate_limit_anon_writes_per_min": 2})
    app = create_app(limited)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://rl.test") as client:
            ok_one = await post_gateway(client, gateway_payload("RL-1"), key="rl-1")
            ok_two = await post_gateway(client, gateway_payload("RL-2"), key="rl-2")
            blocked = await post_gateway(client, gateway_payload("RL-3"), key="rl-3")

    assert ok_one.status_code == 202
    assert ok_two.status_code == 202
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0


async def test_keyed_writes_get_their_own_budget(settings, sessions) -> None:
    """A keyed account is not held to the anonymous IP budget it shares an IP with."""
    import httpx

    from ledgerloop.api.app import create_app

    raw = await mint_key(sessions, "generous-co")
    limited = settings.model_copy(
        update={"rate_limit_anon_writes_per_min": 1, "rate_limit_keyed_writes_per_min": 50}
    )
    app = create_app(limited)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://rl2.test") as client:
            responses = [
                await post_gateway(
                    client, gateway_payload(f"K-{i}"), key=f"k-{i}", headers=key_headers(raw)
                )
                for i in range(5)
            ]
            anon = await post_gateway(client, gateway_payload("A-1"), key="a-1")
            anon_again = await post_gateway(client, gateway_payload("A-2"), key="a-2")

    assert [r.status_code for r in responses] == [202] * 5
    assert anon.status_code == 202
    assert anon_again.status_code == 429


async def test_reads_are_not_write_limited(settings) -> None:
    import httpx

    from ledgerloop.api.app import create_app

    limited = settings.model_copy(update={"rate_limit_anon_writes_per_min": 1})
    app = create_app(limited)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://rl3.test") as client:
            codes = [(await client.get("/v1/stats")).status_code for _ in range(5)]
    assert codes == [200] * 5


# --------------------------------------------------------------------------- #
# Demo retention                                                                #
# --------------------------------------------------------------------------- #


async def test_retention_deletes_idle_demo_tenants_and_their_rows(
    api, sessions, session, settings
) -> None:
    a = session_id()
    occurred = now()
    await post_gateway(
        api, gateway_payload("TXN-OLD", occurred_at=occurred), headers=demo_headers(a)
    )
    await post_ledger(
        api, ledger_payload("TXN-OLD", occurred_at=occurred), headers=demo_headers(a)
    )

    # Age the account rather than the clock: retention reads last_seen_at and nothing
    # else, so this is the whole precondition.
    await _age_demo_accounts(session, hours=48)

    sweeper = Sweeper(sessions, settings.model_copy(update={"demo_retention_hours": 24}))
    deleted = await sweeper.sweep_demo_accounts()
    assert deleted >= 1

    remaining = (
        await session.execute(select(func.count()).select_from(GatewayTransaction))
    ).scalar_one()
    assert remaining == 0
    ledger_rows = (
        await session.execute(select(func.count()).select_from(LedgerEntry))
    ).scalar_one()
    assert ledger_rows == 0


async def test_retention_leaves_real_accounts_alone(api, sessions, session, settings) -> None:
    raw = await mint_key(sessions, "durable-co")
    await post_gateway(api, gateway_payload("TXN-REAL"), headers=key_headers(raw))

    # Age *every* account, real ones included. Only the demo ones may be swept.
    await session.execute(update(Account).values(last_seen_at=text("now() - interval '90 days'")))
    await session.commit()

    sweeper = Sweeper(sessions, settings.model_copy(update={"demo_retention_hours": 24}))
    await sweeper.sweep_demo_accounts()

    survivor = (
        await session.execute(select(Account).where(Account.name == "durable-co"))
    ).scalar_one_or_none()
    assert survivor is not None
    assert survivor.kind is AccountKind.REAL

    rows = (
        await session.execute(
            select(func.count())
            .select_from(GatewayTransaction)
            .where(GatewayTransaction.tenant_id == survivor.id)
        )
    ).scalar_one()
    assert rows == 1

    # And the key still authenticates: the account was not orphaned.
    assert (await api.get("/v1/stats", headers=key_headers(raw))).status_code == 200


async def test_retention_spares_an_active_demo_tenant(api, sessions, session, settings) -> None:
    """last_seen_at is refreshed on every request, so a visitor who is still looking
    at their dashboard is never swept out from under themselves."""
    a = session_id()
    await post_gateway(api, gateway_payload("TXN-ACTIVE"), headers=demo_headers(a))
    await _age_demo_accounts(session, hours=48)

    # One more request, which touches last_seen_at back to now.
    assert (await api.get("/v1/stats", headers=demo_headers(a))).status_code == 200

    sweeper = Sweeper(sessions, settings.model_copy(update={"demo_retention_hours": 24}))
    await sweeper.sweep_demo_accounts()

    still_there = (
        await session.execute(select(func.count()).select_from(GatewayTransaction))
    ).scalar_one()
    assert still_there == 1


async def _age_demo_accounts(session: AsyncSession, hours: int) -> None:
    await session.execute(
        update(Account)
        .where(Account.kind == AccountKind.DEMO)
        .values(last_seen_at=datetime.now(UTC) - timedelta(hours=hours))
    )
    await session.commit()


def test_settings_expose_the_tenancy_knobs() -> None:
    """Cheap guard: these are read by the sweeper and the limiter, and a rename that
    silently reverts one to its default is otherwise invisible until production."""
    settings = Settings()
    assert settings.demo_retention_hours == 24
    assert settings.rate_limit_anon_writes_per_min > 0
    assert settings.rate_limit_keyed_writes_per_min > settings.rate_limit_anon_writes_per_min
    assert IngestSource.GATEWAY.value == "gateway"

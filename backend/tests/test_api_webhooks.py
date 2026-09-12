"""Provider webhook endpoint, against real Postgres and real Redis.

The test that matters most here is the last one: a Stripe-shaped payload, signed the way
Stripe signs it, posted at the URL Stripe would post to, all the way through to a
reconciliation result matched against a ledger entry. Every unit above it can pass while
the wiring between them is wrong.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ledgerloop.db.enums import WebhookDeliveryStatus, WebhookProvider
from ledgerloop.db.models import GatewayTransaction, WebhookSource
from ledgerloop.services.sources import create_source
from ledgerloop.services.tenancy import issue_api_key
from ledgerloop.webhooks.signatures import SIGNATURE_HEADER, sign
from tests.helpers import demo_headers, key_headers, ledger_payload, now, post_ledger
from tests.pipeline import run_pipeline

SECRET = "whsec_a_test_signing_secret"


async def make_source(
    sessions: async_sessionmaker[AsyncSession],
    provider: WebhookProvider = WebhookProvider.STRIPE,
    account: str = "acme",
) -> tuple[str, str]:
    """A keyed account with one webhook source. Returns (source_token, api_key)."""
    async with sessions() as session, session.begin():
        api_key, tenant = await issue_api_key(session, account, label="test")
        source = await create_source(session, tenant, provider, "test source", SECRET)
        return source.source_token, api_key


def stripe_body(
    txn_id: str = "TXN-1", amount: int = 105000, occurred: datetime | None = None
) -> bytes:
    """Serialised once, and the exact bytes are both signed and posted -- which is what
    a provider does. Re-dumping between signing and posting is the bug these tests exist
    to make impossible."""
    created = int((occurred or now()).timestamp())
    return json.dumps(
        {
            "id": f"evt_{txn_id}",
            "type": "payment_intent.succeeded",
            "created": created,
            "data": {
                "object": {
                    "id": f"pi_{txn_id}",
                    "amount": amount,
                    "currency": "inr",
                    "created": created,
                    "metadata": {"txn_id": txn_id},
                }
            },
        }
    ).encode()


async def deliver(
    api, token: str, body: bytes, provider: WebhookProvider = WebhookProvider.STRIPE, **kwargs
):  # type: ignore[no-untyped-def]
    signed_at = kwargs.pop("signed_at", None) or datetime.now(UTC)
    header = kwargs.pop("header", None)
    if header is None:
        header = sign(provider, body, kwargs.pop("secret", SECRET), now=signed_at)
    return await api.post(
        f"/v1/gateway/webhook/{token}",
        content=body,
        headers={"Content-Type": "application/json", SIGNATURE_HEADER[provider]: header},
    )


# --- the happy path ---------------------------------------------------------
async def test_signed_stripe_delivery_is_accepted(api, sessions, session) -> None:
    token, _ = await make_source(sessions)
    response = await deliver(api, token, stripe_body())

    assert response.status_code == 202
    body = response.json()
    assert body["provider"] == "stripe"
    assert body["event_type"] == "payment_intent.succeeded"
    assert body["result"]["duplicate"] is False
    assert body["result"]["txn_id"] == "TXN-1"

    row = (
        await session.execute(
            select(GatewayTransaction).where(GatewayTransaction.txn_id == "TXN-1")
        )
    ).scalar_one()
    # 105000 minor units, stored as major units, exactly.
    assert str(row.amount) == "1050.00"
    assert row.currency == "INR"
    # The provider's own bytes are kept, not our mapping of them: a disputed
    # reconciliation has to be arguable from what Stripe actually sent.
    assert row.raw_payload["data"]["object"]["amount"] == 105000


async def test_redelivery_is_202_duplicate_never_409(api, sessions) -> None:
    """Gateways redeliver on any non-2xx and sometimes on a 2xx. A 409 would make Stripe
    retry harder and eventually disable the endpoint."""
    token, _ = await make_source(sessions)
    body = stripe_body()

    first = await deliver(api, token, body)
    second = await deliver(api, token, body)

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["result"]["duplicate"] is True
    assert second.json()["result"]["row_id"] == first.json()["result"]["row_id"]


async def test_razorpay_delivery_is_accepted(api, sessions) -> None:
    token, _ = await make_source(sessions, WebhookProvider.RAZORPAY, account="rz-co")
    body = json.dumps(
        {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_1",
                        "amount": 250000,
                        "currency": "INR",
                        "created_at": int(now().timestamp()),
                        "notes": {"txn_id": "TXN-RZ"},
                    }
                }
            },
        }
    ).encode()

    response = await deliver(api, token, body, WebhookProvider.RAZORPAY)
    assert response.status_code == 202
    assert response.json()["result"]["txn_id"] == "TXN-RZ"


# --- rejection --------------------------------------------------------------
async def test_wrong_secret_is_401(api, sessions) -> None:
    token, _ = await make_source(sessions)
    response = await deliver(api, token, stripe_body(), secret="whsec_the_wrong_one")
    assert response.status_code == 401


async def test_tampered_body_is_401(api, sessions, session) -> None:
    """Signed at 1050.00, delivered as 9999.99. This is the attack the HMAC is for."""
    token, _ = await make_source(sessions)
    body = stripe_body()
    header = sign(WebhookProvider.STRIPE, body, SECRET, now=datetime.now(UTC))
    tampered = body.replace(b"105000", b"999999")

    response = await deliver(api, token, tampered, header=header)
    assert response.status_code == 401

    stored = (await session.execute(select(GatewayTransaction))).scalars().all()
    assert stored == []


async def test_missing_signature_header_is_401(api, sessions) -> None:
    token, _ = await make_source(sessions)
    response = await api.post(
        f"/v1/gateway/webhook/{token}",
        content=stripe_body(),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 401


async def test_replay_outside_the_tolerance_is_401(api, sessions) -> None:
    """A capture of a genuinely valid request, replayed ten minutes later. The signature
    still verifies; only the timestamp window refuses it."""
    token, _ = await make_source(sessions)
    stale = datetime.now(UTC) - timedelta(minutes=10)
    response = await deliver(api, token, stripe_body(), signed_at=stale)
    assert response.status_code == 401


async def test_replay_inside_the_tolerance_is_accepted(api, sessions) -> None:
    token, _ = await make_source(sessions)
    recent = datetime.now(UTC) - timedelta(minutes=4)
    response = await deliver(api, token, stripe_body(), signed_at=recent)
    assert response.status_code == 202


async def test_unhandled_event_type_is_422(api, sessions) -> None:
    token, _ = await make_source(sessions)
    body = json.dumps({"id": "evt_2", "type": "charge.refunded", "data": {"object": {}}}).encode()
    response = await deliver(api, token, body)
    assert response.status_code == 422


async def test_unknown_and_revoked_tokens_both_404(api, sessions, session) -> None:
    """Same answer for both: telling a caller a token exists but is revoked confirms the
    token, which is the one bit an enumeration attack is after."""
    unknown = await deliver(api, "not-a-real-source-token", stripe_body())
    assert unknown.status_code == 404

    token, _ = await make_source(sessions)
    await session.execute(
        update(WebhookSource)
        .where(WebhookSource.source_token == token)
        .values(revoked_at=datetime.now(UTC))
    )
    await session.commit()

    revoked = await deliver(api, token, stripe_body())
    assert revoked.status_code == 404


async def test_secret_never_appears_in_a_response(api, sessions) -> None:
    token, api_key = await make_source(sessions)
    rejected = await deliver(api, token, stripe_body(), secret="wrong")
    listing = await api.get("/v1/gateway/sources", headers=key_headers(api_key))

    assert SECRET not in rejected.text
    assert SECRET not in listing.text
    assert "signing_secret" not in listing.text


# --- delivery status --------------------------------------------------------
async def test_status_records_success_and_each_failure_mode(api, sessions, session) -> None:
    """The reason this column exists: an endpoint receiving nothing and an endpoint
    rejecting everything both leave the transaction tables empty."""
    token, _ = await make_source(sessions)

    async def status() -> tuple[WebhookDeliveryStatus | None, datetime | None]:
        row = (
            await session.execute(
                select(WebhookSource).where(WebhookSource.source_token == token)
            )
        ).scalar_one()
        await session.refresh(row)
        return row.last_delivery_status, row.last_event_at

    assert await status() == (None, None)

    await deliver(api, token, stripe_body("TXN-OK"))
    delivery_status, seen_at = await status()
    assert delivery_status is WebhookDeliveryStatus.OK
    assert seen_at is not None

    # A rejected delivery must still be recorded -- it is written in its own
    # transaction precisely because the request itself then raises.
    await deliver(api, token, stripe_body("TXN-BAD"), secret="wrong")
    assert (await status())[0] is WebhookDeliveryStatus.INVALID_SIGNATURE

    bad_payload = json.dumps({"id": "e", "type": "charge.refunded", "data": {}}).encode()
    await deliver(api, token, bad_payload)
    assert (await status())[0] is WebhookDeliveryStatus.INVALID_PAYLOAD


# --- source listing ---------------------------------------------------------
async def test_sources_listing_is_scoped_to_the_caller(api, sessions) -> None:
    token, api_key = await make_source(sessions, account="acme")
    _, other_key = await make_source(sessions, account="other-co")

    mine = (await api.get("/v1/gateway/sources", headers=key_headers(api_key))).json()
    theirs = (await api.get("/v1/gateway/sources", headers=key_headers(other_key))).json()

    assert [item["source_token"] for item in mine["items"]] == [token]
    assert token not in [item["source_token"] for item in theirs["items"]]


async def test_demo_tenants_have_no_sources(api) -> None:
    """Not an error -- an empty list, so the dashboard can ask unconditionally and
    simply show nothing when there is nothing to show."""
    response = await api.get("/v1/gateway/sources", headers=demo_headers(
        "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
    ))
    assert response.status_code == 200
    assert response.json()["items"] == []


async def test_listing_reports_last_event_at_after_a_delivery(api, sessions) -> None:
    token, api_key = await make_source(sessions)
    before = (await api.get("/v1/gateway/sources", headers=key_headers(api_key))).json()["items"][0]
    assert before["last_event_at"] is None
    assert before["last_delivery_status"] is None

    await deliver(api, token, stripe_body())

    after = (await api.get("/v1/gateway/sources", headers=key_headers(api_key))).json()["items"][0]
    assert after["last_event_at"] is not None
    assert after["last_delivery_status"] == "ok"


# --- end to end -------------------------------------------------------------
@pytest.mark.parametrize("ledger_amount", ["1050.00"])
async def test_stripe_payload_reaches_a_reconciliation_result(
    api, sessions, stream, settings, ledger_amount
) -> None:
    """The whole path: a Stripe-shaped body, signed as Stripe signs it, posted to the
    token URL, through ingestion and the outbox and the matcher, matched against a
    ledger entry, and visible in that tenant's stats.

    The ledger side is posted with the same API key, which is also the assertion that
    a keyed webhook and a keyed sync land on the same tenant -- if they did not, this
    would reconcile as two separate unmatched breaks.
    """
    token, api_key = await make_source(sessions)
    occurred = now()

    delivered = await deliver(api, token, stripe_body("TXN-E2E", 105000, occurred))
    assert delivered.status_code == 202

    await post_ledger(
        api,
        ledger_payload("TXN-E2E", ledger_amount, occurred_at=occurred),
        headers=key_headers(api_key),
    )

    await run_pipeline(sessions, stream, settings)

    stats = (await api.get("/v1/stats", headers=key_headers(api_key))).json()
    assert stats["matched"] == 1
    assert stats["total"] == 1

    feed = (await api.get("/v1/transactions", headers=key_headers(api_key))).json()["items"]
    assert len(feed) == 1
    assert feed[0]["txn_id"] == "TXN-E2E"
    assert feed[0]["status"] == "matched"
    assert feed[0]["match_layer"] == "exact"
    # Both sides agree on the amount, which is only true if the minor-unit conversion
    # was exact: 105000 -> 1050.00, against a ledger row posted as "1050.00".
    assert feed[0]["gateway_amount"] == "1050.00"
    assert feed[0]["ledger_amount"] == "1050.00"


async def test_a_drifted_stripe_amount_opens_an_exception(api, sessions, stream, settings) -> None:
    """One paisa short on the ledger side, through the provider path. Proves the mapped
    amount reaches layer 3 as a real Decimal rather than something already rounded."""
    token, api_key = await make_source(sessions)
    occurred = now()

    await deliver(api, token, stripe_body("TXN-DRIFT", 105000, occurred))
    await post_ledger(
        api,
        ledger_payload("TXN-DRIFT", "1049.99", occurred_at=occurred),
        headers=key_headers(api_key),
    )
    await run_pipeline(sessions, stream, settings)

    exceptions = (await api.get("/v1/exceptions", headers=key_headers(api_key))).json()["items"]
    assert len(exceptions) == 1
    assert exceptions[0]["status"] == "amount_drift"
    assert exceptions[0]["gateway_amount"] == "1050.00"
    assert exceptions[0]["ledger_amount"] == "1049.99"


# --- dashboard-created sources ---------------------------------------------
async def test_account_can_create_a_source(api, sessions) -> None:
    _, api_key = await make_source(sessions, account="creator-co")
    response = await api.post(
        "/v1/gateway/sources",
        json={"provider": "custom", "label": "from the dashboard"},
        headers=key_headers(api_key),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["provider"] == "custom"
    assert body["last_event_at"] is None
    assert len(body["source_token"]) >= 8
    assert "signing_secret" not in response.text


async def test_demo_tenants_cannot_create_a_source(api) -> None:
    """403, with a reason. A demo endpoint would be deleted after 24h idle and then
    404 at the gateway, which is worse than not offering one."""
    response = await api.post(
        "/v1/gateway/sources",
        json={"provider": "custom"},
        headers=demo_headers("3f2504e0-4f89-11d3-9a0c-0305e82c3301"),
    )
    assert response.status_code == 403
    assert "expire" in response.json()["detail"]


async def test_test_payload_delivers_through_the_real_path(api, sessions, session) -> None:
    token, api_key = await make_source(sessions, account="tester-co")
    source_id = (await api.get("/v1/gateway/sources", headers=key_headers(api_key))).json()[
        "items"
    ][0]["id"]

    response = await api.post(
        f"/v1/gateway/sources/{source_id}/test", headers=key_headers(api_key)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["delivered"] is True
    assert body["http_status"] == 202
    assert body["duplicate"] is False
    assert body["txn_id"].startswith("LEDGERLOOP-TEST-")

    row = (
        await session.execute(
            select(GatewayTransaction).where(GatewayTransaction.txn_id == body["txn_id"])
        )
    ).scalar_one()
    # The synthetic event carries 105000 minor units; a broken conversion shows here.
    assert str(row.amount) == "1050.00"
    assert token  # the source under test is the one created above


async def test_test_payload_reports_a_duplicate_rather_than_failing(api, sessions) -> None:
    """Two tests inside the same second share an event id. That is the idempotency layer
    working, and a dashboard that showed it as an error would teach the operator to
    distrust the guarantee they depend on."""
    _, api_key = await make_source(sessions, account="dupe-co")
    source_id = (await api.get("/v1/gateway/sources", headers=key_headers(api_key))).json()[
        "items"
    ][0]["id"]

    first = await api.post(f"/v1/gateway/sources/{source_id}/test", headers=key_headers(api_key))
    second = await api.post(f"/v1/gateway/sources/{source_id}/test", headers=key_headers(api_key))

    assert first.json()["delivered"] is True
    assert second.json()["delivered"] is True
    if first.json()["txn_id"] == second.json()["txn_id"]:
        assert second.json()["duplicate"] is True


async def test_test_payload_updates_last_event_at(api, sessions) -> None:
    _, api_key = await make_source(sessions, account="stamp-co")
    listing = (await api.get("/v1/gateway/sources", headers=key_headers(api_key))).json()["items"]
    assert listing[0]["last_event_at"] is None

    await api.post(f"/v1/gateway/sources/{listing[0]['id']}/test", headers=key_headers(api_key))

    after = (await api.get("/v1/gateway/sources", headers=key_headers(api_key))).json()["items"][0]
    assert after["last_event_at"] is not None
    assert after["last_delivery_status"] == "ok"


async def test_another_accounts_source_cannot_be_tested(api, sessions) -> None:
    """404, not 403: a 403 confirms the id exists."""
    _, mine = await make_source(sessions, account="a-co")
    _, theirs = await make_source(sessions, account="b-co")
    my_id = (await api.get("/v1/gateway/sources", headers=key_headers(mine))).json()["items"][0][
        "id"
    ]

    response = await api.post(
        f"/v1/gateway/sources/{my_id}/test", headers=key_headers(theirs)
    )
    assert response.status_code == 404

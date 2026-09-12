"""Register a provider webhook endpoint for a real account.

    python -m scripts.create_webhook_source --account acme --provider stripe \
        --signing-secret whsec_... --label "stripe prod"

Command line rather than an endpoint, for the same reason as ``create_api_key``: a
registration endpoint needs its own admin credential, and this runs a handful of times
in a service's life.

``--signing-secret`` is required for stripe and razorpay and must be the value the
provider issued -- the HMAC is computed with their bytes, so anything else fails every
delivery. For ``custom`` there is no external party, so one is generated and printed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from ledgerloop.config import get_settings
from ledgerloop.db.enums import AccountKind, WebhookProvider
from ledgerloop.db.models import Account
from ledgerloop.db.session import build_engine, build_sessionmaker
from ledgerloop.services.sources import create_source
from ledgerloop.services.tenancy import Tenant
from ledgerloop.webhooks.signatures import SIGNATURE_HEADER


async def register(
    account_name: str, provider: WebhookProvider, label: str, signing_secret: str | None
) -> tuple[str, str]:
    settings = get_settings()
    engine = build_engine(settings)
    sessions = build_sessionmaker(engine)
    try:
        async with sessions() as session, session.begin():
            account = (
                await session.execute(select(Account).where(Account.name == account_name))
            ).scalar_one_or_none()
            if account is None:
                raise SystemExit(
                    f"no account named {account_name!r}. Create one with "
                    "`python -m scripts.create_api_key --account "
                    f"{account_name}` first."
                )
            if account.kind is not AccountKind.REAL:
                # A demo account is deleted after 24 hours idle, taking the endpoint
                # with it. Pointing a live gateway at one would work until it silently
                # stopped.
                raise SystemExit(
                    f"account {account_name!r} is a {account.kind.value} account; "
                    "webhook sources belong to real accounts"
                )
            source = await create_source(
                session,
                Tenant(account_id=account.id, name=account.name, kind=account.kind),
                provider,
                label,
                signing_secret,
            )
            return source.source_token, source.signing_secret
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.create_webhook_source",
        description="Register a signed webhook endpoint for an existing real account.",
    )
    parser.add_argument("--account", required=True)
    parser.add_argument(
        "--provider", required=True, choices=[p.value for p in WebhookProvider]
    )
    parser.add_argument("--label", default="default")
    parser.add_argument(
        "--signing-secret",
        default=None,
        help="Required for stripe and razorpay: the secret the provider issued. "
        "Generated for custom.",
    )
    parser.add_argument(
        "--base-url",
        default="https://your-api.example.com",
        help="Only used to print the full URL to paste into the provider's dashboard.",
    )
    args = parser.parse_args()

    provider = WebhookProvider(args.provider)
    if provider is not WebhookProvider.CUSTOM and not args.signing_secret:
        # Generating one here would produce a source that can never verify anything:
        # the provider signs with its own secret, not ours. Better to refuse than to
        # create an endpoint that 401s forever.
        print(
            f"--signing-secret is required for {provider.value}: use the value from "
            "the provider's dashboard, not a generated one.",
            file=sys.stderr,
        )
        return 2

    try:
        token, secret = asyncio.run(
            register(args.account, provider, args.label, args.signing_secret)
        )
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 -- the exit code is the report
        print(f"failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"  URL     : {args.base_url.rstrip('/')}/v1/gateway/webhook/{token}", file=sys.stderr)
    print(f"  header  : {SIGNATURE_HEADER[provider]}", file=sys.stderr)
    if provider is WebhookProvider.CUSTOM:
        print("  secret  : (below, store it now -- sign the raw body with it)", file=sys.stderr)
        print(secret)
    else:
        print("  secret  : as configured in the provider's dashboard", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

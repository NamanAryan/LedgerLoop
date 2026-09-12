"""Issue an API key for a real (non-demo) account.

    python -m scripts.create_api_key --account acme --label "prod webhook"

There is no HTTP endpoint for this on purpose. A key-issuing endpoint needs its own
authentication story -- an admin credential, which is another secret to hold and
rotate -- and this operation happens a handful of times in a service's life. A command
run by whoever already has database access is the smaller surface.

The raw key is printed once, here, and is unrecoverable afterwards: only its SHA-256
is stored. That is the property that makes a database leak not also a credential leak.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from ledgerloop.config import get_settings
from ledgerloop.db.session import build_engine, build_sessionmaker
from ledgerloop.services.tenancy import issue_api_key


async def create(account: str, label: str) -> str:
    settings = get_settings()
    engine = build_engine(settings)
    sessions = build_sessionmaker(engine)
    try:
        async with sessions() as session, session.begin():
            raw, tenant = await issue_api_key(session, account, label)
        return f"{raw}\naccount: {tenant.name} (id {tenant.account_id}, kind {tenant.kind.value})"
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.create_api_key",
        description="Mint an API key for a real account, creating the account if needed.",
    )
    parser.add_argument("--account", required=True, help="Account name. Created if absent.")
    parser.add_argument("--label", default="default", help="Human label for this key.")
    args = parser.parse_args()

    try:
        result = asyncio.run(create(args.account, args.label))
    except Exception as exc:  # noqa: BLE001 -- the exit code is the report
        print(f"failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    # stdout, so the key can be piped straight into a secret store. Everything else
    # this command has to say goes to stderr for the same reason.
    print("Store this now. It is not recoverable.", file=sys.stderr)
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Tenant resolution and API-key handling.

This module answers one question -- "whose data is this request about?" -- and it is
the only place that answers it. Every ``/v1`` route takes the result as a dependency
and passes it down; nothing further in reads a header or looks an account up itself.

Two things are deliberate and worth stating.

**Demo tenancy is isolation, not authentication.** An unauthenticated visitor is
identified by a UUID their own browser generated and sent in ``X-Demo-Session``.
That header is guessable in principle, so it separates one visitor's demo data from
another's and nothing more. It is not a security boundary and must never be used as
one; the UI says so, and nothing real should go through the demo path.

**A bad key never falls through to demo.** If a caller sends ``Authorization`` and it
does not resolve, the answer is 401. Quietly serving them the shared demo tenant
instead would show a broken integration a working-looking dashboard full of somebody
else's synthetic transactions -- which is a far worse failure than an error, because
nobody investigates a screen that looks fine.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ledgerloop.db.enums import AccountKind
from ledgerloop.db.models import Account, ApiKey

#: The shared tenant an unauthenticated caller with no demo session lands on. This is
#: what keeps ``scripts/generate_load`` and ``scripts/benchmark`` working with no
#: arguments changed: they send neither header, so they all land here together and the
#: ground-truth comparison against /v1/stats still sees exactly the rows they posted.
DEFAULT_ACCOUNT_NAME = "default"

#: Prefixed so a leaked key is recognisable in a log or a paste and can be searched
#: for, and so an obviously-not-ours string can be rejected without a database hit.
KEY_PREFIX = "llk_"

#: 32 bytes from the OS CSPRNG. See ``ApiKey`` for why this is hashed with a plain
#: SHA-256 rather than a password KDF.
_KEY_BYTES = 32

#: Characters of the raw key kept in the clear for display. Long enough to tell two
#: keys apart in a list, far too short to narrow a search of a 256-bit space.
_DISPLAY_PREFIX_LEN = 12


@dataclass(frozen=True, slots=True)
class Tenant:
    """The resolved owner of a request. Passed explicitly, never read from a global."""

    account_id: int
    name: str
    kind: AccountKind

    @property
    def is_demo(self) -> bool:
        return self.kind is AccountKind.DEMO


# --------------------------------------------------------------------------- #
# API keys                                                                      #
# --------------------------------------------------------------------------- #


def generate_key() -> str:
    """Mint a new raw key. Returned once, at issue time, and never recoverable."""
    return f"{KEY_PREFIX}{secrets.token_urlsafe(_KEY_BYTES)}"


def hash_key(raw: str) -> str:
    """SHA-256 hex of the raw key.

    Not bcrypt or argon2, and that is a considered choice rather than a shortcut.
    Slow KDFs exist to make *low-entropy human passwords* expensive to guess. These
    keys are 32 bytes of CSPRNG output; there is no dictionary to run against 256 bits
    of entropy, so a deliberate 100ms delay would protect nothing while adding 100ms
    to the authentication path of every authenticated request.
    """
    return hashlib.sha256(raw.encode()).hexdigest()


def key_prefix(raw: str) -> str:
    """The non-secret display fragment stored alongside the hash."""
    return raw[:_DISPLAY_PREFIX_LEN]


def parse_bearer(header: str) -> str | None:
    """Pull the credential out of an ``Authorization`` header.

    Returns None for anything that is not a well-formed bearer token, so the caller
    can answer 401 rather than hashing arbitrary bytes and doing a pointless lookup.
    """
    scheme, separator, credential = header.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return None
    credential = credential.strip()
    return credential or None


async def resolve_api_key(session: AsyncSession, raw_key: str) -> Tenant | None:
    """Resolve a raw key to its account. None means invalid or revoked.

    One indexed probe on the hash, not a scan over ``prefix``. The
    ``compare_digest`` below is belt-and-braces on top of that: the lookup already
    happened in the index, but comparing the fetched hash in constant time keeps this
    function correct if it is ever refactored into a prefix-then-verify shape, which
    is exactly the refactor that introduces a timing oracle when done naively.
    """
    if not raw_key.startswith(KEY_PREFIX):
        return None

    digest = hash_key(raw_key)
    row = (
        await session.execute(
            select(ApiKey, Account)
            .join(Account, ApiKey.account_id == Account.id)
            .where(ApiKey.key_hash == digest)
        )
    ).first()
    if row is None:
        return None

    api_key, account = row
    if not hmac.compare_digest(api_key.key_hash, digest):
        return None
    if api_key.revoked_at is not None:
        return None
    return Tenant(account_id=account.id, name=account.name, kind=account.kind)


async def issue_api_key(
    session: AsyncSession, account_name: str, label: str
) -> tuple[str, Tenant]:
    """Create (or reuse) a real account and mint one key for it. Caller owns the
    transaction. The raw key is returned here and nowhere else, ever again."""
    account = await _upsert_account(session, account_name, AccountKind.REAL)
    if account.kind is not AccountKind.REAL:
        # The name is already taken by a demo tenant. Minting a key for it would hand
        # a customer a credential to an account retention deletes after 24 hours idle.
        raise ValueError(
            f"account {account_name!r} exists as a {account.kind.value} account; "
            "pick another name"
        )
    raw = generate_key()
    session.add(
        ApiKey(
            account_id=account.account_id,
            key_hash=hash_key(raw),
            prefix=key_prefix(raw),
            label=label,
        )
    )
    return raw, account


# --------------------------------------------------------------------------- #
# Demo tenancy                                                                  #
# --------------------------------------------------------------------------- #


def demo_account_name(session_id: str) -> str | None:
    """Validate a ``X-Demo-Session`` value and turn it into an account name.

    Parsed as a UUID rather than accepted as an opaque string, for two reasons: an
    unbounded string is an unbounded number of rows an anonymous caller can create in
    ``accounts``, and normalising through ``UUID`` means the same session written with
    different casing or braces resolves to one tenant rather than several.
    """
    try:
        return f"demo:{UUID(session_id)}"
    except ValueError:
        return None


async def _upsert_account(session: AsyncSession, name: str, kind: AccountKind) -> Tenant:
    """Get-or-create, resolved in one statement rather than SELECT-then-INSERT.

    The check-then-act version races two concurrent first requests from the same demo
    session against each other, and the loser gets an IntegrityError that has to be
    handled anyway. ``ON CONFLICT DO NOTHING`` does both in one statement at the index
    level, so there is no window to lose.
    """
    created = (
        await session.execute(
            pg_insert(Account)
            .values(name=name, kind=kind)
            .on_conflict_do_nothing(index_elements=["name"])
            .returning(Account.id, Account.name, Account.kind)
        )
    ).first()
    if created is not None:
        return Tenant(account_id=created.id, name=created.name, kind=created.kind)

    existing = (
        await session.execute(
            select(Account.id, Account.name, Account.kind).where(Account.name == name)
        )
    ).one()
    return Tenant(account_id=existing.id, name=existing.name, kind=existing.kind)


async def resolve_demo_account(session: AsyncSession, name: str) -> Tenant:
    """The demo tenant for this session name, created on first sight."""
    return await _upsert_account(session, name, AccountKind.DEMO)


async def touch_account(session: AsyncSession, tenant: Tenant, refresh_after_s: int) -> None:
    """Refresh ``last_seen_at``, but only once it has gone stale.

    Retention reads this column, so it has to be kept current or an active visitor's
    data is swept out from under them. Writing it on *every* request would put an
    UPDATE on one hot row in front of every read the dashboard polls -- so the write
    is skipped unless the stored value is already older than the refresh interval.
    The predicate does the skipping in the database: no read-then-write race, and the
    common case is one index probe that matches nothing.
    """
    await session.execute(
        update(Account)
        .where(
            Account.id == tenant.account_id,
            Account.last_seen_at < datetime.now(UTC) - timedelta(seconds=refresh_after_s),
        )
        .values(last_seen_at=text("now()"))
    )

"""multi-tenancy: accounts, api keys, and a tenant column on every scoped table

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-29

Hand-written, like 0001, and for a sharper reason here: the interesting part of this
migration is not the new tables but the *replacement of two uniqueness guarantees*.

``uq_gateway_transactions_idempotency_key`` and its ledger twin enforce ingestion
idempotency globally. The moment a second tenant exists, that is wrong rather than
merely incomplete: idempotency keys are chosen by the client, two tenants will
eventually choose the same string, and ``ON CONFLICT (idempotency_key) DO NOTHING``
would answer the second tenant's genuine payment with ``duplicate: true`` and store
nothing. No error is raised, no log line is written, and the money disappears from
reconciliation. The same argument applies to the four partial unique indexes on
``reconciliation_results``.

So each of those indexes is dropped and recreated with ``tenant_id`` as the *leading*
column, under a new explicit name, and ``downgrade()`` puts the originals back. The
rename is deliberate: an index that means something different should not keep the name
of the thing it replaced, or a future reader diffing production against ``models.py``
sees a match where there is a behaviour change.

Column order matters and is not cosmetic. ``(idempotency_key, tenant_id)`` would still
enforce global uniqueness of the key -- the leading column is the one the constraint
partitions by.

The tenant column is added in three steps within this one migration (nullable ->
backfill -> ``SET NOT NULL``) because an existing deployment has rows, and adding a
non-nullable column with no default to a populated table fails outright. Every
pre-existing row belongs to the shared default demo tenant, which is what the
unauthenticated load generator and benchmark harness keep writing to.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ACCOUNT_KIND = postgresql.ENUM("real", "demo", name="account_kind", create_type=False)

#: The shared tenant every unauthenticated caller without a demo session lands on.
#: Named rather than pinned to an id: ``Identity`` columns make a hard-coded id a
#: sequence-desync waiting to happen, and the resolver looks accounts up by name.
DEFAULT_ACCOUNT_NAME = "default"

#: (table, has a duplicate-count style unique index to rebuild)
TENANTED_TABLES = (
    "gateway_transactions",
    "ledger_entries",
    "reconciliation_results",
    "exceptions",
)


#: (suffix, column, predicate) for the four partial unique indexes that make worker
#: writes idempotent. Named once and used by both directions, so upgrade and downgrade
#: cannot drift apart on a predicate.
_ACTIVE = "IS NOT NULL AND status <> 'duplicate'"
_DUPE = "IS NOT NULL AND status = 'duplicate'"
RESULT_UNIQUE_INDEXES = (
    ("gateway_active", "gateway_txn_id", f"gateway_txn_id {_ACTIVE}"),
    ("ledger_active", "ledger_entry_id", f"ledger_entry_id {_ACTIVE}"),
    ("gateway_duplicate", "gateway_txn_id", f"gateway_txn_id {_DUPE}"),
    ("ledger_duplicate", "ledger_entry_id", f"ledger_entry_id {_DUPE}"),
)


def upgrade() -> None:
    bind = op.get_bind()
    ACCOUNT_KIND.create(bind, checkfirst=True)

    # ----------------------------------------------------------------- accounts
    op.create_table(
        "accounts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("kind", ACCOUNT_KIND, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_accounts")),
    )
    # Tenant resolution on every /v1 request, and the ON CONFLICT target that makes
    # two concurrent first requests from one demo session create a single account.
    op.create_index("uq_accounts_name", "accounts", ["name"], unique=True)
    # Demo retention: WHERE kind = 'demo' AND last_seen_at < :cutoff. Partial, so a
    # growing roster of real accounts never slows the sweep.
    op.create_index(
        "ix_accounts_demo_last_seen_at",
        "accounts",
        ["last_seen_at"],
        postgresql_where=sa.text("kind = 'demo'"),
    )

    # ----------------------------------------------------------------- api_keys
    op.create_table(
        "api_keys",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        # SHA-256 hex. The raw key is never stored, and is unrecoverable after issue.
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        # Non-secret display fragment, so a human can tell two keys apart in a list.
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        # CASCADE: a key has no meaning without its account, unlike reconciliation
        # evidence, which is why the tenant columns below use RESTRICT instead.
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_api_keys_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
    )
    # Authentication is one probe: WHERE key_hash = :h. Unique, because a collision
    # here would mean one credential authenticating two accounts.
    op.create_index("uq_api_keys_key_hash", "api_keys", ["key_hash"], unique=True)
    op.create_index("ix_api_keys_account_id", "api_keys", ["account_id"])

    # The tenant every existing row is backfilled onto, and the one the anonymous
    # load generator and benchmark harness keep using with no arguments changed.
    op.execute(
        sa.text(
            "INSERT INTO accounts (name, kind) VALUES (:name, 'demo') "
            "ON CONFLICT (name) DO NOTHING"
        ).bindparams(name=DEFAULT_ACCOUNT_NAME)
    )

    # ------------------------------------------------- tenant_id, in three steps
    for table in TENANTED_TABLES:
        # 1. Nullable, because the table may already hold rows.
        op.add_column(table, sa.Column("tenant_id", sa.BigInteger(), nullable=True))
        # 2. Backfill. Every pre-existing row is single-tenant history by definition.
        op.execute(
            sa.text(
                f"UPDATE {table} SET tenant_id = "  # noqa: S608 -- table name is a literal above
                "(SELECT id FROM accounts WHERE name = :name)"
            ).bindparams(name=DEFAULT_ACCOUNT_NAME)
        )
        # 3. Now it can carry the invariant the rest of the code relies on.
        op.alter_column(table, "tenant_id", nullable=False)
        # RESTRICT, matching the raw-table FKs: deleting an account must not silently
        # take reconciliation evidence with it. The demo sweeper deletes in explicit
        # dependency order, and a table it forgot fails loudly here instead.
        op.create_foreign_key(
            op.f(f"fk_{table}_tenant_id_accounts"),
            table,
            "accounts",
            ["tenant_id"],
            ["id"],
            ondelete="RESTRICT",
        )

    # --------------------------------------------------- rebuild the uniqueness
    # This block is the reason this migration is hand-written. Each index below is
    # replaced by one that partitions on tenant_id; the predicate is unchanged.

    # Ingestion idempotency. Global -> per-tenant. Without this, two tenants sending
    # the same idempotency key collide and the second one's row is never stored.
    op.drop_index("uq_gateway_transactions_idempotency_key", table_name="gateway_transactions")
    op.create_index(
        "uq_gateway_transactions_tenant_idempotency_key",
        "gateway_transactions",
        ["tenant_id", "idempotency_key"],
        unique=True,
    )
    op.drop_index("uq_ledger_entries_idempotency_key", table_name="ledger_entries")
    op.create_index(
        "uq_ledger_entries_tenant_idempotency_key",
        "ledger_entries",
        ["tenant_id", "idempotency_key"],
        unique=True,
    )

    # Worker idempotency: at most one non-duplicate result per raw row, plus one
    # duplicate marker per raw row. Still enforced by ON CONFLICT DO NOTHING with no
    # conflict target, so the persistence layer needs no change -- whichever of these
    # fires means "already decided".
    for name, column, predicate in RESULT_UNIQUE_INDEXES:
        op.drop_index(
            f"uq_reconciliation_results_{name}", table_name="reconciliation_results"
        )
        op.create_index(
            f"uq_reconciliation_results_tenant_{name}",
            "reconciliation_results",
            ["tenant_id", column],
            unique=True,
            postgresql_where=sa.text(predicate),
        )

    # ------------------------------------------------------ rebuild the read path
    # Not correctness, but every read now carries a tenant predicate, and an index
    # that does not lead with it makes the smallest tenant's dashboard pay for the
    # largest tenant's row count.
    op.drop_index("ix_gateway_transactions_txn_id_occurred_at", table_name="gateway_transactions")
    op.create_index(
        "ix_gateway_transactions_tenant_txn_id_occurred_at",
        "gateway_transactions",
        ["tenant_id", "txn_id", "occurred_at"],
    )
    op.drop_index("ix_ledger_entries_txn_id_occurred_at", table_name="ledger_entries")
    op.create_index(
        "ix_ledger_entries_tenant_txn_id_occurred_at",
        "ledger_entries",
        ["tenant_id", "txn_id", "occurred_at"],
    )
    # The retention sweep deletes by tenant.
    op.create_index("ix_gateway_transactions_tenant_id", "gateway_transactions", ["tenant_id"])
    op.create_index("ix_ledger_entries_tenant_id", "ledger_entries", ["tenant_id"])

    op.drop_index("ix_reconciliation_results_status_id", table_name="reconciliation_results")
    op.create_index(
        "ix_reconciliation_results_tenant_status_id",
        "reconciliation_results",
        ["tenant_id", "status", sa.text("id DESC")],
    )
    op.drop_index("ix_reconciliation_results_resolved_at", table_name="reconciliation_results")
    op.create_index(
        "ix_reconciliation_results_tenant_resolved_at",
        "reconciliation_results",
        ["tenant_id", "resolved_at"],
    )
    # Unfiltered GET /v1/transactions used to ride the primary key. It cannot any
    # more: there is always a tenant predicate now.
    op.create_index(
        "ix_reconciliation_results_tenant_id_id",
        "reconciliation_results",
        ["tenant_id", sa.text("id DESC")],
    )

    op.drop_index("ix_exceptions_open_id", table_name="exceptions")
    op.create_index(
        "ix_exceptions_tenant_open_id",
        "exceptions",
        ["tenant_id", sa.text("id DESC")],
        postgresql_where=sa.text("closed_at IS NULL"),
    )
    op.drop_index("ix_exceptions_closed_id", table_name="exceptions")
    op.create_index(
        "ix_exceptions_tenant_closed_id",
        "exceptions",
        ["tenant_id", sa.text("id DESC")],
        postgresql_where=sa.text("closed_at IS NOT NULL"),
    )
    op.create_index(
        "ix_exceptions_tenant_id_id", "exceptions", ["tenant_id", sa.text("id DESC")]
    )


def downgrade() -> None:
    """Restore the single-tenant schema, original index names included.

    Downgrading is lossy in exactly one way, and it is worth naming: if more than one
    tenant has data, dropping the tenant column collapses them into one namespace and
    the restored global unique indexes will fail to build on the collisions. That is
    the correct outcome -- the alternative would be silently discarding one tenant's
    rows to make the index fit.
    """
    op.drop_index("ix_exceptions_tenant_id_id", table_name="exceptions")
    op.drop_index("ix_exceptions_tenant_closed_id", table_name="exceptions")
    op.create_index(
        "ix_exceptions_closed_id",
        "exceptions",
        [sa.text("id DESC")],
        postgresql_where=sa.text("closed_at IS NOT NULL"),
    )
    op.drop_index("ix_exceptions_tenant_open_id", table_name="exceptions")
    op.create_index(
        "ix_exceptions_open_id",
        "exceptions",
        [sa.text("id DESC")],
        postgresql_where=sa.text("closed_at IS NULL"),
    )

    op.drop_index("ix_reconciliation_results_tenant_id_id", table_name="reconciliation_results")
    op.drop_index(
        "ix_reconciliation_results_tenant_resolved_at", table_name="reconciliation_results"
    )
    op.create_index(
        "ix_reconciliation_results_resolved_at", "reconciliation_results", ["resolved_at"]
    )
    op.drop_index("ix_reconciliation_results_tenant_status_id", table_name="reconciliation_results")
    op.create_index(
        "ix_reconciliation_results_status_id",
        "reconciliation_results",
        ["status", sa.text("id DESC")],
    )

    op.drop_index("ix_ledger_entries_tenant_id", table_name="ledger_entries")
    op.drop_index("ix_gateway_transactions_tenant_id", table_name="gateway_transactions")
    op.drop_index("ix_ledger_entries_tenant_txn_id_occurred_at", table_name="ledger_entries")
    op.create_index(
        "ix_ledger_entries_txn_id_occurred_at", "ledger_entries", ["txn_id", "occurred_at"]
    )
    op.drop_index(
        "ix_gateway_transactions_tenant_txn_id_occurred_at", table_name="gateway_transactions"
    )
    op.create_index(
        "ix_gateway_transactions_txn_id_occurred_at",
        "gateway_transactions",
        ["txn_id", "occurred_at"],
    )

    for name, column, predicate in RESULT_UNIQUE_INDEXES:
        op.drop_index(
            f"uq_reconciliation_results_tenant_{name}", table_name="reconciliation_results"
        )
        op.create_index(
            f"uq_reconciliation_results_{name}",
            "reconciliation_results",
            [column],
            unique=True,
            postgresql_where=sa.text(predicate),
        )

    op.drop_index("uq_ledger_entries_tenant_idempotency_key", table_name="ledger_entries")
    op.create_index(
        "uq_ledger_entries_idempotency_key", "ledger_entries", ["idempotency_key"], unique=True
    )
    op.drop_index(
        "uq_gateway_transactions_tenant_idempotency_key", table_name="gateway_transactions"
    )
    op.create_index(
        "uq_gateway_transactions_idempotency_key",
        "gateway_transactions",
        ["idempotency_key"],
        unique=True,
    )

    for table in reversed(TENANTED_TABLES):
        op.drop_constraint(op.f(f"fk_{table}_tenant_id_accounts"), table, type_="foreignkey")
        op.drop_column(table, "tenant_id")

    op.drop_table("api_keys")
    op.drop_table("accounts")
    ACCOUNT_KIND.drop(op.get_bind(), checkfirst=True)

"""webhook sources: per-account provider endpoints with signing secrets

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-29

Additive only. Nothing existing is dropped or rebuilt, so unlike 0002 this migration
carries no risk to data that is already there -- and the unauthenticated demo path,
which posts to ``/v1/gateway/webhook`` with no token at all, is untouched by it.

``signing_secret`` is stored in the clear. That is not an oversight and it is not the
same decision as ``api_keys.key_hash``: verifying an HMAC requires the secret itself,
so a digest would make verification impossible. The mitigation is elsewhere -- the
column is never logged, never serialised into a response, and never returned by
``GET /v1/gateway/sources``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


WEBHOOK_PROVIDER = postgresql.ENUM(
    "stripe", "razorpay", "custom", name="webhook_provider", create_type=False
)
WEBHOOK_DELIVERY_STATUS = postgresql.ENUM(
    "ok",
    "invalid_signature",
    "invalid_payload",
    name="webhook_delivery_status",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    for enum_type in (WEBHOOK_PROVIDER, WEBHOOK_DELIVERY_STATUS):
        enum_type.create(bind, checkfirst=True)

    op.create_table(
        "webhook_sources",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("provider", WEBHOOK_PROVIDER, nullable=False),
        # In the URL path. Identifies the source and authorises nothing on its own.
        sa.Column("source_token", sa.String(length=64), nullable=False),
        # The secret that actually authenticates a delivery. Never leaves this table.
        sa.Column("signing_secret", sa.String(length=255), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_delivery_status", WEBHOOK_DELIVERY_STATUS, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        # CASCADE, like api_keys: an endpoint has no meaning without its account. The
        # transaction tables it fed keep RESTRICT, because those are evidence.
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_webhook_sources_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_sources")),
    )
    # Delivery resolves the token on every inbound webhook: WHERE source_token = :t.
    # Unique, because a token matching two sources would route one gateway's events
    # into two accounts.
    op.create_index(
        "uq_webhook_sources_source_token", "webhook_sources", ["source_token"], unique=True
    )
    # GET /v1/gateway/sources -> WHERE account_id = :a.
    op.create_index("ix_webhook_sources_account_id", "webhook_sources", ["account_id"])


def downgrade() -> None:
    op.drop_index("ix_webhook_sources_account_id", table_name="webhook_sources")
    op.drop_index("uq_webhook_sources_source_token", table_name="webhook_sources")
    op.drop_table("webhook_sources")
    bind = op.get_bind()
    for enum_type in (WEBHOOK_DELIVERY_STATUS, WEBHOOK_PROVIDER):
        enum_type.drop(bind, checkfirst=True)

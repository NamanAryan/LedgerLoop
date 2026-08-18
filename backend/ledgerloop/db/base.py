"""Declarative base and shared column types."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from sqlalchemy import MetaData, Numeric
from sqlalchemy.orm import DeclarativeBase, mapped_column

# Deterministic constraint names. Without this, PostgreSQL invents names and an
# Alembic downgrade cannot reliably drop what an upgrade created.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

#: Money. numeric(18,2) end to end: exact decimal arithmetic, no binary float error.
#: 18 digits holds ~10^16 major units, far past any realistic settlement volume.
Money = Annotated[Decimal, mapped_column(Numeric(18, 2))]


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

"""Test data builders.

Kept deliberately small: a test that needs a 40-second clock drift should say so in one
argument, not in eight lines of setup, or the assertion gets lost in the scaffolding.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ledgerloop.config import Settings
from ledgerloop.db.enums import IngestSource
from ledgerloop.matching.core import MatchConfig, TxnFacts

#: A fixed instant. Tests that care about time state their offset from this, so a run
#: at 23:59 on new year's eve behaves exactly like a run at noon in June.
T0 = datetime(2026, 3, 14, 9, 30, 0, tzinfo=UTC)

DEFAULT_CONFIG = MatchConfig.from_settings(Settings())


def gateway(
    *,
    row_id: int = 1,
    txn_id: str = "TXN-1",
    amount: str = "1000.00",
    currency: str = "INR",
    offset_s: float = 0.0,
    received_offset_s: float = 0.0,
) -> TxnFacts:
    return TxnFacts(
        side=IngestSource.GATEWAY,
        row_id=row_id,
        txn_id=txn_id,
        amount=Decimal(amount),
        currency=currency,
        occurred_at=T0 + timedelta(seconds=offset_s),
        received_at=T0 + timedelta(seconds=received_offset_s),
    )


def ledger(
    *,
    row_id: int = 2,
    txn_id: str = "TXN-1",
    amount: str = "1000.00",
    currency: str = "INR",
    offset_s: float = 0.0,
    received_offset_s: float = 0.0,
) -> TxnFacts:
    return TxnFacts(
        side=IngestSource.LEDGER,
        row_id=row_id,
        txn_id=txn_id,
        amount=Decimal(amount),
        currency=currency,
        occurred_at=T0 + timedelta(seconds=offset_s),
        received_at=T0 + timedelta(seconds=received_offset_s),
    )

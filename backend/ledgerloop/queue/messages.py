"""The wire format between ingestion and the matcher.

The message is a *pointer plus a hint*, not a copy of the transaction. It carries the
row id; the worker reads the authoritative row from Postgres. If the message carried
the amounts instead, a schema change would mean draining the stream before deploying,
and an in-flight message could reconcile against stale values.

Redis Stream fields are flat string->string, so the envelope is JSON in one field.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ledgerloop.db.enums import IngestSource

FIELD = "data"


@dataclass(frozen=True, slots=True)
class IngestMessage:
    source: IngestSource
    #: Primary key in gateway_transactions or ledger_entries, per ``source``.
    row_id: int
    #: Carried purely so it can be logged without a database round trip.
    txn_id: str
    #: True when ingestion found the idempotency key already present.
    is_duplicate: bool = False
    #: Total submissions seen for this key (1 on first receipt).
    submissions: int = 1

    def to_fields(self) -> dict[str, str]:
        return {
            FIELD: json.dumps(
                {
                    "source": self.source.value,
                    "row_id": self.row_id,
                    "txn_id": self.txn_id,
                    "is_duplicate": self.is_duplicate,
                    "submissions": self.submissions,
                }
            )
        }

    @classmethod
    def from_fields(cls, fields: dict[Any, Any]) -> IngestMessage:
        raw = fields.get(FIELD) or fields.get(FIELD.encode())
        if isinstance(raw, bytes):
            raw = raw.decode()
        body = json.loads(raw)
        return cls(
            source=IngestSource(body["source"]),
            row_id=int(body["row_id"]),
            txn_id=body["txn_id"],
            is_duplicate=bool(body.get("is_duplicate", False)),
            submissions=int(body.get("submissions", 1)),
        )

    def to_payload(self) -> dict[str, Any]:
        """JSONB body stored in outbox_events.payload."""
        return {
            "source": self.source.value,
            "row_id": self.row_id,
            "txn_id": self.txn_id,
            "is_duplicate": self.is_duplicate,
            "submissions": self.submissions,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> IngestMessage:
        return cls(
            source=IngestSource(payload["source"]),
            row_id=int(payload["row_id"]),
            txn_id=payload["txn_id"],
            is_duplicate=bool(payload.get("is_duplicate", False)),
            submissions=int(payload.get("submissions", 1)),
        )

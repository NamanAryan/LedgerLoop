r"""Domain enums.

These are backed by native PostgreSQL enum types. Trade-off, stated plainly:

* Native enum  -> the database rejects a bad value, 4 bytes on disk, and the set
  of legal states is self-documenting in ``\dT+``. Adding a value later needs
  ``ALTER TYPE ... ADD VALUE`` (allowed inside a transaction since PG 12, but the
  new value is not usable until that transaction commits, so it needs its own
  migration).
* ``text`` + CHECK -> trivially evolvable, but weaker typing and no ordering.

The state sets here are fixed by the reconciliation spec, so the migration cost
is a one-off and the type safety is worth it.
"""

from __future__ import annotations

from enum import StrEnum


class IngestSource(StrEnum):
    """Which side of the reconciliation a row came from."""

    GATEWAY = "gateway"
    LEDGER = "ledger"


class ReconStatus(StrEnum):
    """Terminal classification of a reconciliation attempt."""

    MATCHED = "matched"
    UNMATCHED_GATEWAY_ONLY = "unmatched_gateway_only"
    UNMATCHED_LEDGER_ONLY = "unmatched_ledger_only"
    DUPLICATE = "duplicate"
    AMOUNT_DRIFT = "amount_drift"
    # Reserved. Layer 2 (time drift) resolves to MATCHED with match_layer=TIME_DRIFT,
    # because a payment that reconciles 40s late is still a reconciled payment and
    # must count toward the match rate. The status exists so that a future policy
    # ("time drift is an exception, not a match") is a one-line change, not a migration.
    TIME_DRIFT = "time_drift"


class MatchLayer(StrEnum):
    """Which matching layer produced the result. Answers 'why did it match?'."""

    EXACT = "exact"
    TIME_DRIFT = "time_drift"
    AMOUNT_DRIFT = "amount_drift"
    DUPLICATE = "duplicate"
    UNMATCHED_SWEEP = "unmatched_sweep"


#: Statuses that require a human to look at them -> an ``exceptions`` row is opened.
EXCEPTION_STATUSES: frozenset[ReconStatus] = frozenset(
    {
        ReconStatus.AMOUNT_DRIFT,
        ReconStatus.UNMATCHED_GATEWAY_ONLY,
        ReconStatus.UNMATCHED_LEDGER_ONLY,
    }
)

#: Statuses excluded from "active" counts, per the duplicate-suppression rule.
SUPPRESSED_STATUSES: frozenset[ReconStatus] = frozenset({ReconStatus.DUPLICATE})

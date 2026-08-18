"""The matching engine's pure core.

Everything in this module is a function of its arguments. No database, no clock, no
globals, no I/O, no mutation of inputs. The caller fetches the candidate rows and
passes them in; this module decides; a thin wrapper (``ledgerloop.worker.persist``)
writes the decision down.

That split is the whole reason the layers below can be tested exhaustively: a
boundary case at exactly 60.000s is a one-line unit test with no fixtures, no
containers, and no async.

Layer order, each running only on what the previous did not resolve:

  1. exact        same txn_id, same currency, same amount, |dt| <= 2s
  2. time drift   same txn_id, same currency, same amount, |dt| <= 60s
  3. amount drift same txn_id, same currency, |dt| <= 60s,
                  |d amount| <= max(1% of the gateway amount, 10 major units)
  4. duplicate    the same idempotency_key submitted more than once on one side
  5. deferred     nothing matched yet -> return None, the sweeper decides later

A pair that shares a txn_id but breaks currency, or drifts further than layer 3
allows, is deliberately *not* matched. It defers, and the sweeper eventually reports
both sides as unmatched. Silently pairing a 100 USD gateway line with a 100 INR
ledger line would be worse than reporting a break.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from ledgerloop.config import Settings
from ledgerloop.db.enums import IngestSource, MatchLayer, ReconStatus

#: Layer precedence. Lower wins when several counterparties are viable.
_LAYER_RANK: dict[MatchLayer, int] = {
    MatchLayer.EXACT: 0,
    MatchLayer.TIME_DRIFT: 1,
    MatchLayer.AMOUNT_DRIFT: 2,
}


@dataclass(frozen=True, slots=True)
class TxnFacts:
    """The subset of a raw row that matching actually depends on.

    Frozen so a decision can never mutate the thing it is deciding about, and so
    these are safely hashable for use in test parametrisation.
    """

    side: IngestSource
    row_id: int
    txn_id: str
    amount: Decimal
    currency: str
    occurred_at: datetime
    received_at: datetime


@dataclass(frozen=True, slots=True)
class MatchConfig:
    """Tuning knobs, passed explicitly so no function here reads global settings."""

    exact_window: timedelta
    drift_window: timedelta
    amount_drift_pct: Decimal
    amount_drift_abs: Decimal

    @classmethod
    def from_settings(cls, settings: Settings) -> MatchConfig:
        return cls(
            exact_window=timedelta(seconds=settings.exact_window_s),
            drift_window=timedelta(seconds=settings.drift_window_s),
            # via str(): Decimal(0.01) would inherit the float's binary error, which is
            # exactly the class of bug this project exists to catch.
            amount_drift_pct=Decimal(str(settings.amount_drift_pct)),
            amount_drift_abs=Decimal(str(settings.amount_drift_abs)),
        )


@dataclass(frozen=True, slots=True)
class Decision:
    """A terminal classification for one transaction (or pair of them)."""

    status: ReconStatus
    layer: MatchLayer
    gateway_row_id: int | None
    ledger_row_id: int | None
    notes: str | None = None

    @property
    def opens_exception(self) -> bool:
        """Whether a human has to look at this. Derived, never stored twice."""
        return self.status in {
            ReconStatus.AMOUNT_DRIFT,
            ReconStatus.UNMATCHED_GATEWAY_ONLY,
            ReconStatus.UNMATCHED_LEDGER_ONLY,
        }

    @property
    def is_match(self) -> bool:
        return self.status is ReconStatus.MATCHED


# --------------------------------------------------------------------------- #
# Primitives                                                                    #
# --------------------------------------------------------------------------- #


def time_delta(a: TxnFacts, b: TxnFacts) -> timedelta:
    """Absolute distance between two occurrence instants.

    Both are timezone-aware by construction (the schema forbids naive timestamps),
    so this subtraction is always well defined across DST and across regions.
    """
    return abs(a.occurred_at - b.occurred_at)


def amount_tolerance(reference: Decimal, config: MatchConfig) -> Decimal:
    """Layer 3's allowance: 1% of the reference amount, or 10 major units, whichever
    is larger.

    The percentage covers FX/rounding on large tickets; the flat floor covers small
    ones, where 1% of 50 is 0.50 and would flag every trivial fee difference. Both
    arms are Decimal, so the comparison never rounds.
    """
    return max(abs(reference) * config.amount_drift_pct, config.amount_drift_abs)


def _gateway_and_ledger(a: TxnFacts, b: TxnFacts) -> tuple[TxnFacts, TxnFacts]:
    return (a, b) if a.side is IngestSource.GATEWAY else (b, a)


def classify_pair(
    candidate: TxnFacts, other: TxnFacts, config: MatchConfig
) -> tuple[ReconStatus, MatchLayer] | None:
    """Run layers 1-3 against one specific counterparty.

    Returns None when this pair is not matchable at all -- different txn_id,
    different currency, opposite sides missing, or drift beyond every window.
    """
    if candidate.side is other.side:
        return None  # a row never reconciles against its own side
    if candidate.txn_id != other.txn_id:
        return None
    if candidate.currency != other.currency:
        return None  # cross-currency pairing is never a match; see module docstring

    dt = time_delta(candidate, other)
    same_amount = candidate.amount == other.amount

    # Layer 1 -- exact.
    if same_amount and dt <= config.exact_window:
        return ReconStatus.MATCHED, MatchLayer.EXACT

    # Layer 2 -- time drift. Still a match: the money moved, the clocks disagreed.
    if same_amount and dt <= config.drift_window:
        return ReconStatus.MATCHED, MatchLayer.TIME_DRIFT

    # Layer 3 -- amount drift. Not a match: a human confirms the shortfall.
    if not same_amount and dt <= config.drift_window:
        gateway, ledger = _gateway_and_ledger(candidate, other)
        # Tolerance is a percentage *of the gateway amount*: the gateway is the
        # authoritative record of what the customer was actually charged, so the
        # allowance must not stretch when the ledger is the side that is wrong.
        if abs(gateway.amount - ledger.amount) <= amount_tolerance(gateway.amount, config):
            return ReconStatus.AMOUNT_DRIFT, MatchLayer.AMOUNT_DRIFT

    return None


def _sort_key(
    candidate: TxnFacts, other: TxnFacts, layer: MatchLayer, config: MatchConfig
) -> tuple[int, timedelta, Decimal, int]:
    """Best counterparty first: strongest layer, then closest in time, then closest in
    amount, then lowest row id.

    The final row_id term is not cosmetic -- it makes the choice deterministic when
    two counterparties are otherwise indistinguishable, so two workers racing the
    same transaction reach the same conclusion instead of writing conflicting ones.
    """
    return (
        _LAYER_RANK[layer],
        time_delta(candidate, other),
        abs(candidate.amount - other.amount),
        other.row_id,
    )


def decide(
    candidate: TxnFacts,
    counterparties: list[TxnFacts],
    config: MatchConfig,
) -> Decision | None:
    """Layers 1-3 against every available counterparty. None means "defer".

    ``counterparties`` is whatever the caller's query returned -- the "queryable other
    side". This function never asks for more rows, so its behaviour is fully determined
    by what it was handed.
    """
    viable: list[tuple[tuple[int, timedelta, Decimal, int], TxnFacts, ReconStatus, MatchLayer]] = []
    for other in counterparties:
        verdict = classify_pair(candidate, other, config)
        if verdict is None:
            continue
        status, layer = verdict
        viable.append((_sort_key(candidate, other, layer, config), other, status, layer))

    if not viable:
        return None

    _, best, status, layer = min(viable, key=lambda item: item[0])
    gateway, ledger = _gateway_and_ledger(candidate, best)

    notes: str | None = None
    if layer is MatchLayer.TIME_DRIFT:
        drift = time_delta(candidate, best).total_seconds()
        notes = f"clock drift {drift:.3f}s within {config.drift_window.total_seconds():.0f}s window"
    elif layer is MatchLayer.AMOUNT_DRIFT:
        diff = gateway.amount - ledger.amount
        tol = amount_tolerance(gateway.amount, config)
        notes = (
            f"amount drift {diff:+f} (gateway {gateway.amount} vs ledger {ledger.amount}), "
            f"tolerance {tol}"
        )

    return Decision(
        status=status,
        layer=layer,
        gateway_row_id=gateway.row_id,
        ledger_row_id=ledger.row_id,
        notes=notes,
    )


def decide_duplicate(candidate: TxnFacts, submissions: int) -> Decision:
    """Layer 4. The row already existed when this submission arrived.

    Note what is *not* here: no counterparty lookup. A duplicate is decided entirely
    by the idempotency key having been seen before, which the ingestion layer already
    established via the unique constraint. Duplicates are suppressed from active
    counts, so this never competes with layers 1-3 for the same row.
    """
    return Decision(
        status=ReconStatus.DUPLICATE,
        layer=MatchLayer.DUPLICATE,
        gateway_row_id=candidate.row_id if candidate.side is IngestSource.GATEWAY else None,
        ledger_row_id=candidate.row_id if candidate.side is IngestSource.LEDGER else None,
        notes=f"{submissions} submissions of idempotency key; first receipt retained",
    )


def decide_unmatched(candidate: TxnFacts, waited: timedelta) -> Decision:
    """Layer 5. The sweeper gave up waiting for a counterparty.

    Pure, so the sweeper's "how long is too long" policy is unit-testable without
    waiting five real minutes.
    """
    if candidate.side is IngestSource.GATEWAY:
        status = ReconStatus.UNMATCHED_GATEWAY_ONLY
        gateway_row_id, ledger_row_id = candidate.row_id, None
    else:
        status = ReconStatus.UNMATCHED_LEDGER_ONLY
        gateway_row_id, ledger_row_id = None, candidate.row_id

    return Decision(
        status=status,
        layer=MatchLayer.UNMATCHED_SWEEP,
        gateway_row_id=gateway_row_id,
        ledger_row_id=ledger_row_id,
        notes=f"no counterparty after {waited.total_seconds():.0f}s",
    )

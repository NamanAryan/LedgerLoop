"""Prometheus metrics.

The API and the worker are separate processes, so each exposes its own /metrics and
Prometheus scrapes both. No multiprocess collector gymnastics, no shared mmap dir --
two processes, two scrape targets, which is how it would actually be deployed.

Label cardinality is kept deliberately low: every label here has a bounded, small
domain (endpoint names, enum values, exception class names). Nothing is labelled by
txn_id, which would blow up the time series database.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

#: Own registry rather than the global default, so tests can build a clean one and
#: the collectors below are not silently duplicated on module reimport.
REGISTRY = CollectorRegistry(auto_describe=True)

# --- Ingestion (API process) ------------------------------------------------
INGEST_TOTAL = Counter(
    "ledgerloop_ingest_total",
    "Records accepted at the ingestion endpoints.",
    ["endpoint", "outcome"],  # outcome: accepted | duplicate
    registry=REGISTRY,
)
INGEST_REQUEST_SECONDS = Histogram(
    "ledgerloop_ingest_request_seconds",
    "Wall time of an ingestion request, including the outbox write.",
    ["endpoint"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
    registry=REGISTRY,
)

# --- Outbox relay -----------------------------------------------------------
OUTBOX_PUBLISHED_TOTAL = Counter(
    "ledgerloop_outbox_published_total",
    "Outbox rows successfully published to the Redis stream.",
    registry=REGISTRY,
)
OUTBOX_BACKLOG = Gauge(
    "ledgerloop_outbox_backlog",
    "Unpublished outbox rows. Sustained growth means the relay is falling behind.",
    registry=REGISTRY,
)

# --- Queue ------------------------------------------------------------------
QUEUE_DEPTH = Gauge(
    "ledgerloop_queue_depth",
    "Entries in the ingest stream (XLEN).",
    registry=REGISTRY,
)
QUEUE_PENDING = Gauge(
    "ledgerloop_queue_pending",
    "Delivered-but-unacked entries for the consumer group (XPENDING).",
    registry=REGISTRY,
)

# --- Matcher (worker process) ----------------------------------------------
MATCHER_MESSAGES_TOTAL = Counter(
    "ledgerloop_matcher_messages_total",
    "Stream messages processed by the matcher, by outcome.",
    ["status", "layer"],  # layer="deferred" when no decision was reached yet
    registry=REGISTRY,
)
MATCHER_DURATION_SECONDS = Histogram(
    "ledgerloop_matcher_duration_seconds",
    "Time to process one stream message end to end (read -> decide -> persist).",
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
    registry=REGISTRY,
)
MATCH_LATENCY_SECONDS = Histogram(
    "ledgerloop_match_latency_seconds",
    "Reconciliation latency: ingestion of the later side -> result written.",
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 300.0),
    registry=REGISTRY,
)

# --- Sweeper ----------------------------------------------------------------
SWEEPER_MARKED_TOTAL = Counter(
    "ledgerloop_sweeper_marked_total",
    "Rows the unmatched sweeper moved to a terminal state.",
    ["status"],
    registry=REGISTRY,
)
SWEEPER_RUNS_TOTAL = Counter(
    "ledgerloop_sweeper_runs_total",
    "Completed sweeper passes.",
    registry=REGISTRY,
)

# --- Errors -----------------------------------------------------------------
ERRORS_TOTAL = Counter(
    "ledgerloop_errors_total",
    "Errors by component and exception class.",
    ["component", "type"],
    registry=REGISTRY,
)


def record_error(component: str, exc: BaseException) -> None:
    ERRORS_TOTAL.labels(component=component, type=type(exc).__name__).inc()

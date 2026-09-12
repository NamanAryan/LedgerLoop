"""Environment-driven configuration.

Every knob the engine has lives here. Nothing reads ``os.environ`` directly, so a
test can build a ``Settings`` object with overrides and never touch the process
environment.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated

from pydantic import Field, PostgresDsn, RedisDsn, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LEDGERLOOP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Infrastructure -------------------------------------------------
    database_url: PostgresDsn = Field(
        default="postgresql+asyncpg://ledgerloop:ledgerloop@localhost:5432/ledgerloop",
        description=(
            "Async SQLAlchemy DSN. A bare postgres:// or postgresql:// scheme is "
            "upgraded to postgresql+asyncpg://; any other driver is rejected."
        ),
    )
    redis_url: RedisDsn = Field(default="redis://localhost:6379/0")

    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_echo: bool = False

    @field_validator("database_url", mode="before")
    @classmethod
    def _require_async_driver(cls, value: object) -> object:
        """Pin the DSN to asyncpg, upgrading a managed provider's scheme if needed.

        Render, Railway, Heroku and Neon all inject ``postgresql://`` (Heroku still
        emits the older ``postgres://``), and none of them offer a way to rewrite it
        on the way out. SQLAlchemy reads a bare scheme as psycopg2 -- a *sync* driver,
        which would block the event loop under load and surface as mysterious latency
        rather than as a configuration error.

        Rewriting the scheme is not the silent fallback this project refuses. The
        fallback worth refusing is falling *back* to sync, and an explicitly sync DSN
        is still rejected below -- loudly, at startup, which is the only moment anyone
        can act on it.
        """
        if not isinstance(value, str):
            return value
        scheme, separator, rest = value.partition("://")
        if not separator:
            return value
        if scheme in {"postgres", "postgresql"}:
            return f"postgresql+asyncpg://{rest}"
        if scheme.startswith("postgresql+") and scheme != "postgresql+asyncpg":
            raise ValueError(
                f"database_url must use the asyncpg driver, got {scheme!r}. The engine "
                "is async end to end; a sync driver would block the event loop."
            )
        return value

    # --- Redis Stream ---------------------------------------------------
    stream_key: str = "ledgerloop:ingest"
    consumer_group: str = "matchers"
    # XREADGROUP block timeout. Shorter = faster SIGTERM response, more idle round trips.
    stream_block_ms: int = 2_000
    stream_batch_size: int = 64
    # XADD MAXLEN ~ cap. The stream is a transport, not storage; Postgres is the record.
    stream_maxlen: int = 1_000_000

    # How long an entry may sit unacked before another consumer may claim it.
    # Must comfortably exceed the time to process one message, or healthy workers
    # steal each other's in-flight work.
    claim_min_idle_ms: int = 30_000
    claim_interval_s: float = 10.0

    # --- Outbox relay ---------------------------------------------------
    outbox_batch_size: int = 500
    outbox_poll_interval_s: float = 0.2

    # --- Matching windows -----------------------------------------------
    exact_window_s: int = 2  # layer 1: |gateway.occurred_at - ledger.occurred_at| <= 2s
    drift_window_s: int = 60  # layers 2 and 3
    amount_drift_pct: Annotated[float, Field(gt=0)] = 0.01  # 1%
    amount_drift_abs: Annotated[float, Field(gt=0)] = 10.0  # or 10 major units, whichever is larger

    # --- Sweeper ---------------------------------------------------------
    sweep_interval_s: int = 30
    unmatched_after_s: int = 300  # 5 min: how long a row waits for its counterparty

    # --- Tenancy ----------------------------------------------------------
    #: How long an idle demo tenant and all of its rows are kept. Demo data is
    #: synthetic by definition -- the path is unauthenticated, so nothing real is
    #: supposed to be on it -- and without retention every visitor who ever opened the
    #: dashboard leaves rows behind forever.
    demo_retention_hours: int = 24
    #: How often retention runs. Far less often than ``sweep_interval_s``: the
    #: unmatched sweep is answering a question about money and has to be prompt, while
    #: retention is housekeeping with a 24-hour deadline. Running it on every sweep
    #: pass would put an index scan of ``accounts`` between the matcher and its work
    #: 120 times an hour to delete nothing.
    demo_sweep_interval_s: int = 3_600
    #: Only rewrite ``accounts.last_seen_at`` once it is this stale. Retention reads
    #: that column, so it must stay current, but writing it on every request would put
    #: an UPDATE of one hot row in front of every dashboard poll.
    last_seen_refresh_s: int = 300

    #: Writes per minute from one IP with no API key. 1,000/s, which is a deliberate
    #: compromise: the benchmark harness offers up to 1,000 tx/s from a single host
    #: over the same unauthenticated path, so a tighter default would silently turn
    #: documented benchmark runs into a graph of 429s. It still bounds the open
    #: endpoint -- an anonymous caller cannot stream into the database indefinitely --
    #: and a public deployment that is not being benchmarked should lower it.
    rate_limit_anon_writes_per_min: int = 60_000
    #: Writes per minute for a keyed account. Present rather than unlimited so a
    #: runaway retry loop in a customer's integration is capped somewhere, but set far
    #: above any real webhook volume.
    rate_limit_keyed_writes_per_min: int = 120_000

    # --- Ingestion limits -------------------------------------------------
    ledger_batch_max: int = 1_000

    # --- Worker process ---------------------------------------------------
    # The relay and sweeper ride along in the worker container by default. Both can be
    # switched off so they can be run as their own deployments instead; nothing in
    # either depends on being co-located with the matcher.
    enable_relay: bool = True
    enable_sweeper: bool = True
    #: Matcher consume loops inside one worker process. Horizontal scaling is by
    #: container count; this exists to use a single container's connection pool fully.
    worker_concurrency: int = 1
    worker_metrics_port: int = 9100

    #: Run the matcher (and whichever of relay/sweeper are enabled) inside the API
    #: process instead of as their own deployment.
    #:
    #: Off by default, and it should stay off anywhere that can afford a second
    #: process. It gives up exactly what worker/main.py exists to provide: a slow
    #: match now shares the API's event loop, so a matching backlog shows up as slow
    #: webhook responses, and the gateway starts retrying transactions that were never
    #: lost. The two also stop scaling independently.
    #:
    #: It exists because free hosting tiers price a second always-on process at more
    #: than zero, and a demo that runs is worth more than a topology that doesn't. The
    #: honest framing is that this is a deployment concession, not a design: the code
    #: path is identical, only the process boundary moves.
    #:
    #: Requires a single API worker process. With `uvicorn --workers N` each worker
    #: would start its own matcher and relay -- correctness survives that (the consumer
    #: group and the partial unique indexes both hold) but it multiplies the polling
    #: for no gain.
    embed_worker: bool = False

    # --- API edge ---------------------------------------------------------
    #: Browser origins allowed to call the read path. The dashboard is served from a
    #: different origin than the API in every deployment shape we support (Vite on
    #: :5173 locally, a static host in production), so CORS is not optional. Listed
    #: explicitly rather than "*": the read path is not public data, and a wildcard
    #: would also forbid credentialed requests if auth is ever added.
    #:
    #: ``NoDecode`` is load-bearing. Without it pydantic-settings runs ``json.loads``
    #: on the raw env var *before* any validator sees it, because the field is a list;
    #: a plain ``https://ledgerloop-web.onrender.com`` then dies at startup with
    #: "Expecting value: line 1 column 1". NoDecode hands the string through untouched
    #: so ``_split_origins`` below can accept both forms.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            # Vite binds the IPv6 loopback on Windows and prints this form, so a dev
            # who opens the URL it printed arrives with this Origin. Omitting it fails
            # the preflight with a bare 400 and no Allow-Origin header, which the
            # browser reports only as an opaque network error.
            "http://[::1]:5173",
        ],
        description="Comma-separated in the environment; JSON list also accepted.",
    )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        # Accept the plain comma-separated form as well as a JSON list, because a
        # comma-separated string is what a Render or Railway env var looks like.
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                return json.loads(text)
            return [item.strip() for item in text.split(",") if item.strip()]
        return value

    # --- Observability ----------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = True
    service_name: str = "ledgerloop"

    @property
    def sync_database_url(self) -> str:
        """psycopg DSN, used only by tooling that cannot speak asyncpg."""
        return str(self.database_url).replace("+asyncpg", "+psycopg")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

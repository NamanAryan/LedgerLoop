"""Environment-driven configuration.

Every knob the engine has lives here. Nothing reads ``os.environ`` directly, so a
test can build a ``Settings`` object with overrides and never touch the process
environment.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import Field, PostgresDsn, RedisDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # --- API edge ---------------------------------------------------------
    #: Browser origins allowed to call the read path. The dashboard is served from a
    #: different origin than the API in every deployment shape we support (Vite on
    #: :5173 locally, a static host in production), so CORS is not optional. Listed
    #: explicitly rather than "*": the read path is not public data, and a wildcard
    #: would also forbid credentialed requests if auth is ever added.
    cors_origins: list[str] = Field(
        default=["http://localhost:5173", "http://127.0.0.1:5173"],
        description="Comma-separated in the environment; JSON list also accepted.",
    )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        # pydantic-settings parses list fields as JSON. Accept the plain
        # comma-separated form too, because that is what a Railway env var looks like.
        if isinstance(value, str) and not value.strip().startswith("["):
            return [item.strip() for item in value.split(",") if item.strip()]
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

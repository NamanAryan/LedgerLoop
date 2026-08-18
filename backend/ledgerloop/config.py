"""Environment-driven configuration.

Every knob the engine has lives here. Nothing reads ``os.environ`` directly, so a
test can build a ``Settings`` object with overrides and never touch the process
environment.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import Field, PostgresDsn, RedisDsn
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
        description="Async SQLAlchemy DSN. Must use the asyncpg driver.",
    )
    redis_url: RedisDsn = Field(default="redis://localhost:6379/0")

    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_echo: bool = False

    # --- Redis Stream ---------------------------------------------------
    stream_key: str = "ledgerloop:ingest"
    consumer_group: str = "matchers"
    # XREADGROUP block timeout. Shorter = faster SIGTERM response, more idle round trips.
    stream_block_ms: int = 2_000
    stream_batch_size: int = 64
    # XADD MAXLEN ~ cap. The stream is a transport, not storage; Postgres is the record.
    stream_maxlen: int = 1_000_000

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

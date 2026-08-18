"""Structured logging.

JSON by default because these logs are meant to be queried, not read. The worker
emits one line per message with message_id / txn_id / decision / layer / duration_ms,
which is enough to reconstruct any single transaction's history from the log alone.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from ledgerloop.config import Settings, get_settings

_configured = False


def configure_logging(settings: Settings | None = None) -> None:
    """Idempotent: safe to call from both the API and worker entry points."""
    global _configured
    if _configured:
        return
    settings = settings or get_settings()

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
    )
    # uvicorn installs its own handlers; let everything flow through structlog's
    # ProcessorFormatter path instead of double-printing.
    for noisy in ("uvicorn.access", "uvicorn.error"):
        logging.getLogger(noisy).handlers.clear()
        logging.getLogger(noisy).propagate = True

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer()
        if settings.log_json
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.log_level.upper(), logging.INFO)
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]

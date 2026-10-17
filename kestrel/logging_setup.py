"""structlog configuration: rotating JSON lines under %LOCALAPPDATA%\\Kestrel\\logs. Kept small
for Phase 0; the frozen-app stdout/stderr repair lives in the desktop entry point (Phase 14)."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

import structlog

from kestrel import paths

_configured = False


def configure_logging(level: str = "INFO") -> None:
    global _configured
    if _configured:
        return
    paths.ensure_dirs()
    handler = RotatingFileHandler(
        paths.logs_dir() / "kestrel.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.setLevel(level.upper())
    root.addHandler(handler)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True

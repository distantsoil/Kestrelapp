"""Logging for the app: one rotating kestrel.log under the data home's logs folder.

Every Kestrel module logs through structlog. Without `structlog.configure` those events go to
stdout, which a windowed (packaged) build does not have, so they were silently lost: the desktop
entry point configured only stdlib logging, and a diagnostics bundle held third-party lines and not
one Kestrel event. Here structlog renders each event as JSON and hands it to stdlib logging, so the
app's own events and the stdlib loggers (uvicorn, httpx, alembic) share one file and one format.
The frozen-app stdout/stderr repair still lives in the desktop entry point."""

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
    # The same line prefix the desktop log always had, so stdlib lines keep their time and thread
    # and a structlog event (a JSON object, with its own UTC timestamp) reads in the same stream.
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s"))
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
            # ensure_ascii=False: the diagnostics scrubber masks secrets and the confidential
            # employer by exact substring, and the default escapes "Nestlé" to "Nestlé",
            # which it would never match. Text is written as it was logged; the file is UTF-8.
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.stdlib.LoggerFactory(),
        # Not cached: a module-level logger that logged once before this ran (during imports in
        # the frozen entry point) would otherwise keep the stdout default for the whole process.
        cache_logger_on_first_use=False,
    )
    _configured = True

"""Logging for the app: one rotating kestrel.log under the data home's logs folder.

Every Kestrel module logs through structlog. Without `structlog.configure` those events go to
stdout, which a windowed (packaged) build does not have, so they were silently lost: the desktop
entry point configured only stdlib logging, and a diagnostics bundle held third-party lines and not
one Kestrel event. Here structlog renders each event as JSON and hands it to stdlib logging, so the
app's own events and the stdlib loggers (uvicorn, httpx, alembic) share one file and one format.
The frozen-app stdout/stderr repair still lives in the desktop entry point."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler

import structlog

from kestrel import paths

_configured = False

# httpx logs every request line at INFO with its full URL. A board that takes its key in the query
# string (Adzuna's app_id and app_key) would write the key into kestrel.log, so any credential-like
# query parameter is masked on its way to the file.
_QUERY_SECRET = re.compile(
    r"([?&](?:app_id|app_key|api_key|apikey|key|token|access_token)=)[^&\s\"']+", re.IGNORECASE
)


class _MaskQuerySecrets(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "=" in message:
            masked = _QUERY_SECRET.sub(r"\1***", message)
            if masked != message:
                record.msg, record.args = masked, None
        return True


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
    handler.addFilter(_MaskQuerySecrets())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(level.upper())
    root.addHandler(handler)
    # httpx logs one INFO line per request (the model, the embeddings, the tray's status poll), and
    # 805 of one user's last 2000 log lines were those, which pushed the import out of the
    # diagnostics bundle (#105). Warnings and errors from both still reach the file.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

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

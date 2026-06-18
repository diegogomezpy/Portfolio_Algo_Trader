"""Shared structured JSON logger for sharpe-engine.

Every module logs through :func:`get_logger`. File output is one JSON object
per line (machine-parseable for audit), rotated daily and kept for 30 days
per the ARCHITECTURE logging decision. A concise human-readable line is also
emitted to stderr for interactive runs.

Arbitrary structured context attaches via the standard ``extra=`` mechanism::

    log = get_logger(__name__)
    log.info("ingest complete", extra={"date": "2026-06-18", "symbols": 4812})

Any keys passed in ``extra`` that are not standard ``LogRecord`` attributes
are serialized into the JSON record, so downstream tooling can filter on them.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# Default log directory (git-ignored). Overridable for tests via SHARPE_LOG_DIR.
_LOG_DIR = Path(os.environ.get("SHARPE_LOG_DIR", "logs"))
_LOG_FILE = "sharpe-engine.log"
_RETENTION_DAYS = 30

# LogRecord attributes that are NOT user-supplied context — everything else in
# a record's __dict__ is treated as structured extra and serialized to JSON.
_STANDARD_ATTRS = frozenset(
    logging.makeLogRecord({}).__dict__.keys()
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """Render a :class:`logging.LogRecord` as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Attach any structured context passed via extra=.
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = _json_safe(value)
        return json.dumps(payload, default=str)


def _json_safe(value: object) -> object:
    """Best-effort conversion of an extra value to something JSON-serializable."""
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, (list, tuple, dict)):
        return value
    return str(value)


def get_logger(name: str) -> logging.Logger:
    """Return a configured logger for ``name`` (idempotent per process).

    File handler writes JSON to ``logs/sharpe-engine.log``, rotating at
    midnight and retaining 30 days. A stderr handler emits a concise line.
    Calling this repeatedly for the same name does not stack handlers.
    """
    logger = logging.getLogger(name)
    if getattr(logger, "_sharpe_configured", False):
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False

    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    file_handler = TimedRotatingFileHandler(
        _LOG_DIR / _LOG_FILE, when="midnight", backupCount=_RETENTION_DAYS, utc=True
    )
    file_handler.setFormatter(JsonFormatter())
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")
    )
    logger.addHandler(stream_handler)

    logger._sharpe_configured = True  # type: ignore[attr-defined]
    return logger

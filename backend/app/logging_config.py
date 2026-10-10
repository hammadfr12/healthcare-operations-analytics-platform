"""Structured application logging.

Logs are emitted as single-line ``key=value`` records so they stay greppable in
a terminal and parseable by a log shipper. Row-level operational data is never
logged: the demo dataset is synthetic, but the pipeline treats it as if it were
sensitive so the same code path is safe against a real, de-identified extract.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

# Attribute names present on every LogRecord; anything else came from `extra=`.
_RESERVED = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
) | {"message", "asctime", "taskName"}


class KeyValueFormatter(logging.Formatter):
    """Render a log record as ``timestamp level logger message k=v ...``."""

    def format(self, record: logging.LogRecord) -> str:
        base = (
            f"ts={datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec='milliseconds')} "
            f"level={record.levelname.lower()} logger={record.name} "
            f"msg={json.dumps(record.getMessage())}"
        )
        extras: dict[str, Any] = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED and not key.startswith("_")
        }
        if extras:
            rendered = " ".join(
                f"{key}={json.dumps(value, default=str)}"
                for key, value in sorted(extras.items())
            )
            base = f"{base} {rendered}"
        if record.exc_info:
            base = f"{base} exc={json.dumps(self.formatException(record.exc_info))}"
        return base


def configure_logging(level: str = "INFO") -> None:
    """Install the structured formatter on the root logger (idempotent)."""
    root = logging.getLogger()
    root.setLevel(level.upper())

    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(KeyValueFormatter())
    root.addHandler(handler)

    # Uvicorn installs its own handlers; route them through ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

    # DuckDB is chatty at DEBUG; keep it at WARNING regardless of app level.
    logging.getLogger("duckdb").setLevel(logging.WARNING)

"""DuckDB connection management.

The API opens a short-lived **read-only** connection per request. DuckDB
supports multiple concurrent readers on one file, and a read-only connection
cannot mutate the analytical store from a request handler. It also means an
in-flight request keeps reading the database it opened even if the pipeline
swaps in a new one — which is exactly the "last known-good output" behaviour
the reliability requirement asks for.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

import duckdb

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


class DatabaseUnavailable(RuntimeError):
    """Raised when the analytical store has not been built yet."""


@contextmanager
def connect(
    settings: Settings | None = None, *, read_only: bool = True
) -> Iterator[duckdb.DuckDBPyConnection]:
    """Yield a DuckDB connection, closing it on exit."""
    settings = settings or get_settings()
    path = settings.database_file

    if read_only and not path.exists():
        raise DatabaseUnavailable(
            f"The analytical store does not exist at {path}. "
            "Build it with `make pipeline` (or POST /api/admin/refresh)."
        )

    connection = duckdb.connect(str(path), read_only=read_only)
    try:
        # Bound how much memory a single analytical query may claim so one
        # heavy request cannot starve the process on a developer laptop.
        connection.execute("SET memory_limit = '1GB'")
        yield connection
    finally:
        connection.close()


def database_exists(settings: Settings | None = None) -> bool:
    """True when the analytical store file is present."""
    settings = settings or get_settings()
    return settings.database_file.exists()

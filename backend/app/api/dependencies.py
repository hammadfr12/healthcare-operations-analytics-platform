"""Shared FastAPI dependencies.

Two things every data route needs: a connection to the analytical store, and
the application settings. Both are provided as generators so FastAPI closes the
connection when the request finishes, even if the handler raises.
"""

from __future__ import annotations

from collections.abc import Iterator

import duckdb
from fastapi import Depends, HTTPException, status

from app.config import Settings, get_settings
from app.db import DatabaseUnavailable, connect


def settings_dependency() -> Settings:
    """The cached application settings."""
    return get_settings()


def connection_dependency(
    settings: Settings = Depends(settings_dependency),
) -> Iterator[duckdb.DuckDBPyConnection]:
    """A short-lived read-only DuckDB connection for one request.

    A missing store is a 503 rather than a 500: the service is up, the data
    simply has not been built yet, and the caller can fix that with a refresh.
    """
    try:
        with connect(settings) as connection:
            yield connection
    except DatabaseUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


Connection = Depends(connection_dependency)
SettingsDep = Depends(settings_dependency)

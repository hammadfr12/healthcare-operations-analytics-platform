"""Service metadata routes: health, readiness, dimensions, freshness.

These are the endpoints a dashboard calls before it renders anything: what
filters are available, what date range the data covers, and whether the store
is fresh enough to trust.
"""

from __future__ import annotations

import duckdb
from fastapi import APIRouter, Depends

from app import analytics
from app.api.dependencies import connection_dependency, settings_dependency
from app.api.schemas import HealthResponse, ReadinessResponse
from app.config import Settings
from app.db import database_exists

router = APIRouter(tags=["meta"])

API_VERSION = "1.0.0"


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
def health(settings: Settings = Depends(settings_dependency)) -> HealthResponse:
    """Cheap liveness check. Never touches the database file."""
    return HealthResponse(
        status="ok",
        database_available=database_exists(settings),
        environment=settings.environment,
        version=API_VERSION,
    )


@router.get("/ready", response_model=ReadinessResponse, summary="Readiness probe")
def ready(
    settings: Settings = Depends(settings_dependency),
    conn: duckdb.DuckDBPyConnection = Depends(connection_dependency),
) -> ReadinessResponse:
    """Confirm the analytical store exists and has a completed pipeline run."""
    freshness = analytics.data_freshness(conn, settings.stale_after_hours)
    return ReadinessResponse(
        status="ready" if freshness.get("last_refresh_status") == "succeeded" else "degraded",
        database_path=str(settings.database_file),
        pipeline_status=freshness.get("last_refresh_status"),
        last_refresh_at=(
            freshness["last_refresh_at"].isoformat()
            if freshness.get("last_refresh_at")
            else None
        ),
        data_start_date=(
            freshness["data_start_date"].isoformat()
            if freshness.get("data_start_date")
            else None
        ),
        data_end_date=(
            freshness["data_end_date"].isoformat()
            if freshness.get("data_end_date")
            else None
        ),
    )


@router.get("/dimensions", summary="Filterable dimension values")
def dimensions(
    conn: duckdb.DuckDBPyConnection = Depends(connection_dependency),
) -> dict[str, list[dict[str, object]]]:
    """Every department, facility, visit type and status actually present."""
    return analytics.list_dimensions(conn)


@router.get("/freshness", summary="Data freshness and coverage")
def freshness(
    settings: Settings = Depends(settings_dependency),
    conn: duckdb.DuckDBPyConnection = Depends(connection_dependency),
) -> dict[str, object]:
    """When the store was last refreshed, and whether that counts as stale."""
    return analytics.data_freshness(conn, settings.stale_after_hours)


@router.get("/bounds", summary="Dataset date bounds")
def bounds(
    conn: duckdb.DuckDBPyConnection = Depends(connection_dependency),
) -> dict[str, str | None]:
    """Earliest and latest date in the appointment mart, for defaulting the UI."""
    start, end = analytics.dataset_bounds(conn)
    return {
        "start_date": start.isoformat() if start else None,
        "end_date": end.isoformat() if end else None,
    }

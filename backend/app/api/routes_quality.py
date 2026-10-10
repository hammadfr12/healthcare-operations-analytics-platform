"""Data quality routes.

The Data Quality page reads from here. Row counts, run history and issue
breakdowns all come from the ``quality`` schema the pipeline writes on every
refresh, so the page can show failing runs as well as successful ones.
"""

from __future__ import annotations

from typing import Any

import duckdb
from fastapi import APIRouter, Depends, Query

from app import analytics
from app.api.dependencies import connection_dependency

router = APIRouter(prefix="/quality", tags=["quality"])

Connection = Depends(connection_dependency)


@router.get("/runs", summary="Recent pipeline runs")
def runs(
    conn: duckdb.DuckDBPyConnection = Connection,
    limit: int = Query(default=10, ge=1, le=100),
) -> dict[str, Any]:
    """Run history, newest first, including any that failed."""
    return {"items": analytics.quality_runs(conn, limit=limit)}


@router.get("/latest", summary="Most recent pipeline run")
def latest(
    conn: duckdb.DuckDBPyConnection = Connection,
) -> dict[str, Any] | None:
    """The latest run, or ``null`` when the store has never been built."""
    return analytics.latest_quality_run(conn)


@router.get("/issues", summary="Issues for a run")
def issues(
    conn: duckdb.DuckDBPyConnection = Connection,
    run_id: str | None = Query(
        default=None, description="Defaults to the most recent run."
    ),
) -> dict[str, Any]:
    """Row-level findings, grouped by category."""
    return {
        "run_id": run_id,
        "items": analytics.quality_issues(conn, run_id=run_id),
    }


@router.get("/issue-categories", summary="Issue counts by category and severity")
def issue_categories(
    conn: duckdb.DuckDBPyConnection = Connection,
    run_id: str | None = Query(
        default=None, description="Defaults to the most recent run."
    ),
) -> dict[str, Any]:
    """Summary counts for the Data Quality header cards."""
    return {
        "run_id": run_id,
        "items": analytics.quality_issue_categories(conn, run_id=run_id),
    }


@router.get("/row-counts", summary="Row counts per analytical table")
def row_counts(
    conn: duckdb.DuckDBPyConnection = Connection,
) -> dict[str, Any]:
    """What is actually loaded, so the UI can show the store's contents."""
    return {"items": analytics.table_row_counts(conn)}

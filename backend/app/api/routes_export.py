"""CSV export routes.

The exports are the same numbers the dashboard shows, produced by the same
functions — the mart extract is filtered by the same
:meth:`FilterSet.appointment_predicates` the KPI cards use, so a downloaded file
can never disagree with the screen it came from.

Every export carries an ``X-Export-Filters`` response header describing the
window and selection it was generated under, because a CSV that has lost its
filter context is a misleading artifact.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator
from typing import Any

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from app import analytics, metrics
from app.api.dependencies import connection_dependency
from app.filters import FilterSet, get_filters

router = APIRouter(prefix="/export", tags=["export"])

Filters = Depends(get_filters)
Connection = Depends(connection_dependency)

# Fixed allow-list: a caller can choose a table, never name one.
MART_TABLES: dict[str, str] = {
    "appointments": "analytics.mart_appointments_daily",
    "encounters": "analytics.mart_encounters_daily",
    "capacity": "analytics.mart_capacity_daily",
    "flow": "analytics.mart_flow_distribution_daily",
}

# Exports are capped so one request cannot stream the whole store into memory.
MAX_EXPORT_ROWS = 250_000


def _csv_response(
    rows: Iterator[tuple[Any, ...]],
    header: list[str],
    *,
    filename: str,
    filters: FilterSet,
) -> StreamingResponse:
    """Stream rows out as CSV with a filter-context header."""

    def generate() -> Iterator[str]:
        buffer = io.StringIO()
        writer = csv.writer(buffer)

        def flush() -> str:
            value = buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
            return value

        writer.writerow(header)
        yield flush()
        for row in rows:
            writer.writerow(row)
            yield flush()

    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Export-Filters": json.dumps(filters.as_dict),
            "X-Export-Filename": filename,
        },
    )


@router.get("/kpis.csv", summary="KPI set as CSV")
def export_kpis(
    filters: FilterSet = Filters,
    conn: duckdb.DuckDBPyConnection = Connection,
) -> StreamingResponse:
    """One row per KPI, with its definition, sample size and change figure."""
    computed = metrics.build_kpi_set(conn, filters)
    header = [
        "key", "label", "value", "unit", "numerator", "denominator",
        "sample_size", "available", "unavailable_reason", "definition",
        "caveat", "comparison_value", "change_percent", "change_available",
        "change_unavailable_reason",
    ]

    def rows() -> Iterator[tuple[Any, ...]]:
        for metric in computed.values():
            row = metric.to_dict()
            yield tuple(row.get(column) for column in header)

    return _csv_response(
        rows(), header,
        filename=f"kpis_{filters.start_date}_{filters.end_date}.csv",
        filters=filters,
    )


@router.get("/departments.csv", summary="Department comparison as CSV")
def export_departments(
    filters: FilterSet = Filters,
    conn: duckdb.DuckDBPyConnection = Connection,
) -> StreamingResponse:
    """The department table, flattened (the nested comparison block is prefixed)."""
    items = analytics.department_performance(conn, filters)
    header = [
        "department_id", "department_name", "specialty_group",
        "appointments_scheduled", "appointments_terminal", "appointments_completed",
        "cancelled", "no_show", "cancellation_rate", "no_show_rate",
        "lost_slot_rate", "encounters", "completed_visits", "median_wait_minutes",
        "mean_wait_minutes", "wait_valid_count", "wait_invalid_count",
        "mean_visit_duration_minutes", "appointment_lead_time_days",
        "capacity_utilisation", "capacity_days_observed", "capacity_available",
        "rankable", "rankable_reason",
        "comparison_cancellation_rate", "comparison_cancellation_rate_change_percent",
        "comparison_no_show_rate", "comparison_no_show_rate_change_percent",
        "comparison_mean_wait_minutes", "comparison_mean_wait_change_percent",
    ]

    def flatten(item: dict[str, Any]) -> dict[str, Any]:
        flat = {key: value for key, value in item.items() if key != "comparison"}
        for key, value in (item.get("comparison") or {}).items():
            if key.endswith("_reason"):
                continue
            flat[f"comparison_{key}"] = value
        return flat

    def rows() -> Iterator[tuple[Any, ...]]:
        for item in items:
            flat = flatten(item)
            yield tuple(flat.get(column) for column in header)

    return _csv_response(
        rows(), header,
        filename=f"departments_{filters.start_date}_{filters.end_date}.csv",
        filters=filters,
    )


@router.get("/marts/{table}.csv", summary="Raw daily mart extract as CSV")
def export_mart(
    table: str,
    filters: FilterSet = Filters,
    conn: duckdb.DuckDBPyConnection = Connection,
) -> StreamingResponse:
    """A filtered daily mart extract, for Tableau or a spreadsheet.

    ``table`` is resolved against a fixed allow-list, so no request input can
    reach the query as a table name.
    """
    if table not in MART_TABLES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Unknown table {table!r}. Available: "
                f"{', '.join(sorted(MART_TABLES))}."
            ),
        )

    relation = MART_TABLES[table]

    # Each mart carries the same filterable dimensions but a different date
    # column name is not needed: all four expose `metric_date`.
    where, params = filters.appointment_predicates()
    count = conn.execute(
        f"SELECT COUNT(*) FROM {relation} WHERE {where}", params
    ).fetchone()
    total = int(count[0]) if count else 0
    if total > MAX_EXPORT_ROWS:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"This selection matches {total:,} rows, above the "
                f"{MAX_EXPORT_ROWS:,}-row export limit. Narrow the date range "
                "or add a dimension filter."
            ),
        )

    cursor = conn.execute(
        f"SELECT * FROM {relation} WHERE {where} ORDER BY metric_date", params
    )
    header = [column[0] for column in cursor.description]

    def rows() -> Iterator[tuple[Any, ...]]:
        while True:
            batch = cursor.fetchmany(5_000)
            if not batch:
                return
            yield from batch

    return _csv_response(
        rows(), header,
        filename=f"{table}_{filters.start_date}_{filters.end_date}.csv",
        filters=filters,
    )

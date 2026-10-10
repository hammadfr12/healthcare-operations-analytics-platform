"""Dashboard metric routes.

Every route takes the shared :func:`app.filters.get_filters` dependency, so the
KPI cards, the charts and the department table all resolve the same window from
the same query string.
"""

from __future__ import annotations

from typing import Any

import duckdb
from fastapi import APIRouter, Depends, Query

from app import analytics, metrics
from app.api.dependencies import connection_dependency
from app.api.schemas import KpiResponse
from app.filters import FilterSet, get_filters

router = APIRouter(tags=["metrics"])

Filters = Depends(get_filters)
Connection = Depends(connection_dependency)


@router.get("/kpis", response_model=KpiResponse, summary="KPI cards with period-over-period change")
def kpis(
    filters: FilterSet = Filters,
    conn: duckdb.DuckDBPyConnection = Connection,
    include_comparison: bool = Query(
        default=True,
        description="Compute the preceding equal-length window for change figures.",
    ),
) -> KpiResponse:
    """The headline KPI set.

    A metric whose denominator is zero comes back with ``value: null`` and
    ``available: false`` rather than 0 — "0%" and "undefined" are different
    claims. ``sample_size`` travels with every aggregate so the UI can qualify
    it.
    """
    computed = metrics.build_kpi_set(conn, filters, include_comparison=include_comparison)
    return KpiResponse(
        filters=filters.as_dict,
        metrics=[metric.to_dict() for metric in computed.values()],
    )


@router.get("/trend", summary="Appointment and encounter trend")
def trend(
    filters: FilterSet = Filters,
    conn: duckdb.DuckDBPyConnection = Connection,
    granularity: str = Query(
        default="week",
        pattern="^(day|week|month)$",
        description="Bucket size. The date spine comes from dim_date, so a day "
                    "with no activity appears as an explicit zero.",
    ),
) -> dict[str, Any]:
    """Time series for the main chart, at day, week or month grain."""
    return {
        "filters": filters.as_dict,
        "granularity": granularity,
        "points": analytics.trend_series(conn, filters, granularity),
    }


@router.get("/status-mix", summary="Appointment status distribution")
def status_mix(
    filters: FilterSet = Filters,
    conn: duckdb.DuckDBPyConnection = Connection,
) -> dict[str, Any]:
    """Completed / cancelled / no-show / open split for the window."""
    return {
        "filters": filters.as_dict,
        "items": analytics.status_mix(conn, filters),
    }


@router.get("/visit-type-mix", summary="Volume by visit type")
def visit_type_mix(
    filters: FilterSet = Filters,
    conn: duckdb.DuckDBPyConnection = Connection,
) -> dict[str, Any]:
    """Appointment volume per visit type, with the outcome split within each."""
    return {
        "filters": filters.as_dict,
        "items": analytics.visit_type_mix(conn, filters),
    }


@router.get("/cancellation-reasons", summary="Cancellations by reason and initiator")
def cancellation_reasons(
    filters: FilterSet = Filters,
    conn: duckdb.DuckDBPyConnection = Connection,
) -> dict[str, Any]:
    """Reason breakdown, split by whether patient or provider initiated."""
    return {
        "filters": filters.as_dict,
        "items": analytics.cancellation_reasons(conn, filters),
    }


@router.get("/patient-flow", summary="Wait-time and flow summary")
def patient_flow(
    filters: FilterSet = Filters,
    conn: duckdb.DuckDBPyConnection = Connection,
) -> dict[str, Any]:
    """Median, mean, p90 and p95 wait, plus the full 15-minute histogram."""
    return {
        "filters": filters.as_dict,
        "summary": analytics.patient_flow_summary(conn, filters),
    }


@router.get("/wait-trend", summary="Wait time over time")
def wait_trend(
    filters: FilterSet = Filters,
    conn: duckdb.DuckDBPyConnection = Connection,
    granularity: str = Query(default="week", pattern="^(day|week|month)$"),
) -> dict[str, Any]:
    """Median and mean wait per period, with the valid sample size each period."""
    return {
        "filters": filters.as_dict,
        "granularity": granularity,
        "points": analytics.wait_trend(conn, filters, granularity),
    }


@router.get("/departments", summary="Per-department operational comparison")
def departments(
    filters: FilterSet = Filters,
    conn: duckdb.DuckDBPyConnection = Connection,
    include_comparison: bool = Query(default=True),
) -> dict[str, Any]:
    """Comparable metrics per department.

    Departments below the minimum terminal sample are returned with
    ``rankable: false`` and a reason, rather than being hidden or ranked on
    noise.
    """
    return {
        "filters": filters.as_dict,
        "items": analytics.department_performance(
            conn, filters, include_comparison=include_comparison
        ),
    }

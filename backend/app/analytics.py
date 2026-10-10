"""Analytical query services behind the dashboard views.

Each function returns plain dictionaries ready for the Pydantic response
models. Keeping the SQL here (rather than in the route handlers) means the CSV
export and the Tableau export call exactly the same code as the dashboard.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Any

import duckdb

from app.filters import FilterSet, GRANULARITIES
from app.metrics import (
    MIN_TERMINAL_SAMPLE,
    MIN_WAIT_SAMPLE,
    EncounterAggregate,
    _safe_rate,
    aggregate_capacity,
    aggregate_encounters,
    median_lead_time_days,
    median_wait_minutes,
    percentile_wait_minutes,
    period_over_period_change,
)

logger = logging.getLogger(__name__)

# Human-readable labels for the controlled vocabularies. Kept here (not in the
# frontend) so the API and the Tableau extracts label values identically.
STATUS_LABELS: dict[str, str] = {
    "completed": "Completed",
    "cancelled": "Cancelled",
    "no_show": "No show",
    "scheduled": "Scheduled (open)",
    "unscheduled": "Unscheduled (walk-in)",
}

VISIT_TYPE_LABELS: dict[str, str] = {
    "new": "New",
    "follow_up": "Follow-up",
    "procedure": "Procedure",
    "urgent": "Urgent",
    "unscheduled": "Unscheduled",
}

CANCELLATION_REASON_LABELS: dict[str, str] = {
    "patient_unavailable": "Patient unavailable",
    "patient_rescheduled": "Patient rescheduled",
    "transport": "Transport / access",
    "provider_unavailable": "Provider unavailable",
    "schedule_change": "Schedule change",
    "facility_closure": "Facility closure",
    "equipment_unavailable": "Equipment unavailable",
}


def _label(mapping: dict[str, str], key: str | None) -> str:
    if key is None:
        return "Unknown"
    return mapping.get(key, key.replace("_", " ").title())


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def dataset_bounds(conn: duckdb.DuckDBPyConnection) -> tuple[date | None, date | None]:
    """Earliest and latest date present in the appointment mart."""
    row = conn.execute(
        "SELECT MIN(metric_date), MAX(metric_date) FROM analytics.mart_appointments_daily"
    ).fetchone()
    if row is None or row[0] is None:
        return None, None
    return row[0], row[1]


def list_dimensions(conn: duckdb.DuckDBPyConnection) -> dict[str, list[dict[str, Any]]]:
    """Enumerate every filterable dimension value actually present in the data."""
    departments = [
        {
            "value": row[0],
            "label": row[1],
            "specialty_group": row[2],
            "facility_id": row[3],
        }
        for row in conn.execute(
            """
            SELECT d.department_id, d.department_name, d.specialty_group, f.facility_id
            FROM core.dim_department AS d
            LEFT JOIN (
                SELECT DISTINCT department_id, facility_id FROM core.fct_appointments
            ) AS f ON f.department_id = d.department_id
            ORDER BY d.department_name
            """
        ).fetchall()
    ]

    facilities = [
        {"value": row[0], "label": row[1]}
        for row in conn.execute(
            "SELECT facility_id, facility_name FROM core.dim_facility ORDER BY facility_id"
        ).fetchall()
    ]

    visit_types = [
        {"value": row[0], "label": _label(VISIT_TYPE_LABELS, row[0]), "count": row[1]}
        for row in conn.execute(
            """
            SELECT visit_type, SUM(appointment_count)
            FROM analytics.mart_appointments_daily
            GROUP BY visit_type ORDER BY 2 DESC
            """
        ).fetchall()
    ]

    statuses = [
        {"value": row[0], "label": _label(STATUS_LABELS, row[0]), "count": row[1]}
        for row in conn.execute(
            """
            SELECT appointment_status, SUM(appointment_count)
            FROM analytics.mart_appointments_daily
            GROUP BY appointment_status ORDER BY 2 DESC
            """
        ).fetchall()
    ]

    return {
        "departments": departments,
        "facilities": facilities,
        "visit_types": visit_types,
        "statuses": statuses,
    }


def latest_quality_run(conn: duckdb.DuckDBPyConnection) -> dict[str, Any] | None:
    """Most recent pipeline run, or ``None`` when the store has never been built."""
    try:
        row = conn.execute(
            """
            SELECT run_id, started_at, completed_at, status,
                   input_rows, accepted_rows, rejected_rows, error_summary
            FROM quality.data_quality_runs
            ORDER BY completed_at DESC
            LIMIT 1
            """
        ).fetchone()
    except duckdb.Error:
        return None

    if row is None:
        return None

    try:
        summary = json.loads(row[7]) if row[7] else {}
    except json.JSONDecodeError:  # pragma: no cover - defensive
        summary = {}

    return {
        "run_id": row[0],
        "started_at": row[1],
        "completed_at": row[2],
        "status": row[3],
        "input_rows": row[4],
        "accepted_rows": row[5],
        "rejected_rows": row[6],
        "error_summary": summary,
    }


def data_freshness(conn: duckdb.DuckDBPyConnection, stale_after_hours: int) -> dict[str, Any]:
    """Report when the data was last refreshed and whether that is stale."""
    run = latest_quality_run(conn)
    min_date, max_date = dataset_bounds(conn)

    if run is None:
        return {
            "last_refresh_at": None,
            "last_refresh_status": "unknown",
            "age_hours": None,
            "is_stale": True,
            "stale_after_hours": stale_after_hours,
            "data_start_date": min_date,
            "data_end_date": max_date,
            "message": "No pipeline run has been recorded. The dashboard may be empty.",
        }

    completed = run["completed_at"]
    if isinstance(completed, datetime):
        completed_utc = completed.replace(tzinfo=timezone.utc) if completed.tzinfo is None else completed
    else:  # pragma: no cover - defensive
        completed_utc = datetime.now(timezone.utc)

    age_hours = (datetime.now(timezone.utc) - completed_utc).total_seconds() / 3600.0
    is_stale = age_hours > stale_after_hours

    if run["rejected_rows"] > 0:
        message = (
            f"Last refresh completed with {run['rejected_rows']:,} rejected row(s). "
            "Review the Data Quality page."
        )
    elif is_stale:
        message = (
            f"Data was last refreshed {age_hours:.1f} hours ago, beyond the "
            f"{stale_after_hours}-hour freshness threshold."
        )
    else:
        message = f"Data refreshed {age_hours:.1f} hours ago."

    return {
        "last_refresh_at": completed,
        "last_refresh_status": run["status"],
        "age_hours": round(age_hours, 2),
        "is_stale": is_stale,
        "stale_after_hours": stale_after_hours,
        "data_start_date": min_date,
        "data_end_date": max_date,
        "message": message,
    }


# ---------------------------------------------------------------------------
# Trends
# ---------------------------------------------------------------------------

def trend_series(
    conn: duckdb.DuckDBPyConnection, filters: FilterSet, granularity: str
) -> list[dict[str, Any]]:
    """Appointment and encounter metrics at day/week/month grain.

    The date spine comes from ``core.dim_date`` (LEFT JOINed) so a day with no
    activity appears as an explicit zero rather than vanishing from the chart
    and creating a false straight line between its neighbours.
    """
    if granularity not in GRANULARITIES:
        granularity = "day"
    bucket_expr = f"date_trunc('{granularity}', dd.date_day)"

    where_a, params_a = filters.appointment_predicates("a")
    where_e, params_e = filters.encounter_predicates("e")

    sql = f"""
    WITH spine AS (
        SELECT DISTINCT {bucket_expr} AS bucket
        FROM core.dim_date AS dd
        WHERE dd.date_day BETWEEN ? AND ?
    ),
    appt AS (
        SELECT
            {bucket_expr} AS bucket,
            SUM(a.appointment_count) AS appointments,
            SUM(a.terminal_count)    AS terminal,
            SUM(a.completed_count)   AS completed,
            SUM(a.cancelled_count)   AS cancelled,
            SUM(a.no_show_count)     AS no_show,
            SUM(a.open_count)        AS open_count
        FROM analytics.mart_appointments_daily AS a
        JOIN core.dim_date AS dd ON dd.date_day = a.metric_date
        WHERE {where_a}
        GROUP BY 1
    ),
    enc AS (
        SELECT
            {bucket_expr} AS bucket,
            SUM(e.encounter_count)       AS encounters,
            SUM(e.completed_visit_count) AS completed_visits,
            SUM(e.wait_valid_count)      AS wait_valid_count,
            SUM(e.wait_sum_minutes)      AS wait_sum_minutes,
            SUM(e.wait_invalid_count)    AS wait_invalid_count
        FROM analytics.mart_encounters_daily AS e
        JOIN core.dim_date AS dd ON dd.date_day = e.metric_date
        WHERE {where_e}
        GROUP BY 1
    )
    SELECT
        spine.bucket,
        COALESCE(appt.appointments, 0),
        COALESCE(appt.terminal, 0),
        COALESCE(appt.completed, 0),
        COALESCE(appt.cancelled, 0),
        COALESCE(appt.no_show, 0),
        COALESCE(appt.open_count, 0),
        COALESCE(enc.encounters, 0),
        COALESCE(enc.completed_visits, 0),
        COALESCE(enc.wait_valid_count, 0),
        COALESCE(enc.wait_sum_minutes, 0.0),
        COALESCE(enc.wait_invalid_count, 0)
    FROM spine
    LEFT JOIN appt ON appt.bucket = spine.bucket
    LEFT JOIN enc  ON enc.bucket  = spine.bucket
    ORDER BY spine.bucket
    """

    params = [filters.start_date, filters.end_date, *params_a, *params_e]
    rows = conn.execute(sql, params).fetchall()

    series: list[dict[str, Any]] = []
    for row in rows:
        (
            bucket, appointments, terminal, completed, cancelled, no_show, open_count,
            encounters, completed_visits, wait_valid, wait_sum, wait_invalid,
        ) = row

        mean_wait = (wait_sum / wait_valid) if wait_valid else None
        series.append(
            {
                "period_start": bucket.date() if hasattr(bucket, "date") else bucket,
                "label": _format_period_label(bucket, granularity),
                "granularity": granularity,
                "appointments_scheduled": int(appointments),
                "appointments_terminal": int(terminal),
                "appointments_completed": int(completed),
                "appointments_cancelled": int(cancelled),
                "appointments_no_show": int(no_show),
                "appointments_open": int(open_count),
                "cancellation_rate": _safe_rate(cancelled, terminal),
                "no_show_rate": _safe_rate(no_show, terminal),
                "completed_visits": int(completed_visits),
                "encounters": int(encounters),
                "wait_sample_size": int(wait_valid),
                "wait_invalid_count": int(wait_invalid),
                "mean_wait_minutes": mean_wait,
            }
        )
    return series


def _format_period_label(bucket: Any, granularity: str) -> str:
    """Short axis label for a period bucket."""
    stamp = bucket.date() if hasattr(bucket, "date") else bucket
    if granularity == "month":
        return stamp.strftime("%b %Y")
    if granularity == "week":
        return f"w/c {stamp.strftime('%d %b')}"
    return stamp.strftime("%d %b")


# ---------------------------------------------------------------------------
# Status mix / visit-type mix / cancellation reasons
# ---------------------------------------------------------------------------

def status_mix(conn: duckdb.DuckDBPyConnection, filters: FilterSet) -> list[dict[str, Any]]:
    """Appointment status distribution for the selected window."""
    where, params = filters.appointment_predicates("m")
    rows = conn.execute(
        f"""
        SELECT m.appointment_status, SUM(m.appointment_count)
        FROM analytics.mart_appointments_daily AS m
        WHERE {where}
        GROUP BY 1
        ORDER BY 2 DESC
        """,
        params,
    ).fetchall()
    total = sum(row[1] for row in rows) or 0
    return [
        {
            "status": row[0],
            "label": _label(STATUS_LABELS, row[0]),
            "count": int(row[1]),
            "share_percent": _safe_rate(row[1], total),
        }
        for row in rows
    ]


def visit_type_mix(conn: duckdb.DuckDBPyConnection, filters: FilterSet) -> list[dict[str, Any]]:
    """Appointment volume by visit type, with the outcome split within each."""
    where, params = filters.appointment_predicates("m")
    rows = conn.execute(
        f"""
        SELECT
            m.visit_type,
            SUM(m.appointment_count),
            SUM(m.completed_count),
            SUM(m.cancelled_count),
            SUM(m.no_show_count),
            SUM(m.terminal_count)
        FROM analytics.mart_appointments_daily AS m
        WHERE {where}
        GROUP BY 1
        ORDER BY 2 DESC
        """,
        params,
    ).fetchall()
    total = sum(row[1] for row in rows) or 0
    return [
        {
            "visit_type": row[0],
            "label": _label(VISIT_TYPE_LABELS, row[0]),
            "count": int(row[1]),
            "completed": int(row[2]),
            "cancelled": int(row[3]),
            "no_show": int(row[4]),
            "terminal": int(row[5]),
            "share_percent": _safe_rate(row[1], total),
            "cancellation_rate": _safe_rate(row[3], row[5]),
            "no_show_rate": _safe_rate(row[4], row[5]),
        }
        for row in rows
    ]


def cancellation_reasons(conn: duckdb.DuckDBPyConnection, filters: FilterSet) -> list[dict[str, Any]]:
    """Cancellation volume grouped by reason category and initiation side.

    The PRD requires it to be explicit whether patient- and provider-initiated
    cancellations are combined. They are combined in the headline rate, and
    separated here so the split is always visible.
    """
    # fct_appointments dates on `appointment_date`, not the mart's `metric_date`,
    # so this needs the fact-table predicate builder rather than the mart one.
    where, params = filters.fact_appointment_predicates("a")
    rows = conn.execute(
        f"""
        SELECT
            COALESCE(a.cancellation_reason_category, 'unspecified') AS reason,
            COUNT(*)
        FROM core.fct_appointments AS a
        WHERE {where} AND a.is_cancelled
        GROUP BY 1
        ORDER BY 2 DESC
        """,
        params,
    ).fetchall()
    total = sum(row[1] for row in rows) or 0

    patient_side = {"patient_unavailable", "patient_rescheduled", "transport"}
    provider_side = {"provider_unavailable", "schedule_change"}
    facility_side = {"facility_closure", "equipment_unavailable"}

    def side(reason: str) -> str:
        if reason in patient_side:
            return "patient"
        if reason in provider_side:
            return "provider"
        if reason in facility_side:
            return "facility"
        return "unspecified"

    return [
        {
            "reason": row[0],
            "label": _label(CANCELLATION_REASON_LABELS, row[0]),
            "initiated_by": side(row[0]),
            "count": int(row[1]),
            "share_percent": _safe_rate(row[1], total),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Patient flow
# ---------------------------------------------------------------------------

def wait_distribution(conn: duckdb.DuckDBPyConnection, filters: FilterSet) -> list[dict[str, Any]]:
    """Wait-time histogram over the 15-minute buckets built in the flow mart."""
    where, params = filters.encounter_predicates("m")
    rows = conn.execute(
        f"""
        SELECT
            m.wait_bucket_lower_minutes,
            ANY_VALUE(m.wait_bucket_label),
            SUM(m.encounter_count)
        FROM analytics.mart_flow_distribution_daily AS m
        WHERE {where}
        GROUP BY 1
        ORDER BY 1
        """,
        params,
    ).fetchall()
    total = sum(row[2] for row in rows) or 0
    return [
        {
            "bucket_lower_minutes": int(row[0]),
            "label": row[1],
            "count": int(row[2]),
            "share_percent": _safe_rate(row[2], total),
        }
        for row in rows
    ]


def patient_flow_summary(conn: duckdb.DuckDBPyConnection, filters: FilterSet) -> dict[str, Any]:
    """Everything the Patient Flow page needs in one response."""
    enc: EncounterAggregate = aggregate_encounters(conn, filters)
    median_wait, valid_n = median_wait_minutes(conn, filters)
    percentiles = percentile_wait_minutes(conn, filters)
    mean_wait = (enc.wait_sum_minutes / enc.wait_valid_count) if enc.wait_valid_count else None
    mean_duration = (
        enc.duration_sum_minutes / enc.duration_valid_count if enc.duration_valid_count else None
    )

    return {
        "median_wait_minutes": median_wait,
        "mean_wait_minutes": mean_wait,
        "p90_wait_minutes": percentiles.get("p90"),
        "p95_wait_minutes": percentiles.get("p95"),
        "wait_valid_count": enc.wait_valid_count,
        "wait_invalid_count": enc.wait_invalid_count,
        "wait_negative_count": enc.wait_negative_count,
        "wait_implausible_count": enc.wait_implausible_count,
        "mean_visit_duration_minutes": mean_duration,
        "visit_duration_valid_count": enc.duration_valid_count,
        "visit_duration_outlier_count": enc.duration_outlier_count,
        "walk_in_count": enc.walk_ins,
        "completed_visit_count": enc.completed_visits,
        "encounter_count": enc.encounters,
        "distribution": wait_distribution(conn, filters),
        "wait_sample_sufficient": enc.wait_valid_count >= MIN_WAIT_SAMPLE,
    }


def wait_trend(
    conn: duckdb.DuckDBPyConnection, filters: FilterSet, granularity: str = "week"
) -> list[dict[str, Any]]:
    """Median and mean wait over time, with the valid sample size per period.

    Median is computed per bucket from the fact table because it cannot be
    recovered from daily sums.
    """
    if granularity not in GRANULARITIES:
        granularity = "day"
    where, params = filters.fact_encounter_predicates("e")
    rows = conn.execute(
        f"""
        SELECT
            date_trunc('{granularity}', e.encounter_date) AS bucket,
            MEDIAN(e.wait_minutes)                        AS median_wait,
            AVG(e.wait_minutes)                           AS mean_wait,
            COUNT(*) FILTER (WHERE e.wait_minutes IS NOT NULL) AS valid_n,
            COUNT(*) FILTER (WHERE e.wait_invalid_reason IS NOT NULL) AS invalid_n
        FROM core.fct_encounters AS e
        WHERE {where}
        GROUP BY 1
        ORDER BY 1
        """,
        params,
    ).fetchall()
    return [
        {
            "period_start": row[0].date() if hasattr(row[0], "date") else row[0],
            "label": _format_period_label(row[0], granularity),
            "median_wait_minutes": float(row[1]) if row[1] is not None else None,
            "mean_wait_minutes": float(row[2]) if row[2] is not None else None,
            "valid_sample_size": int(row[3]),
            "invalid_count": int(row[4]),
            "sample_sufficient": int(row[3]) >= MIN_WAIT_SAMPLE,
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Department performance
# ---------------------------------------------------------------------------

def department_performance(
    conn: duckdb.DuckDBPyConnection, filters: FilterSet, *, include_comparison: bool = True
) -> list[dict[str, Any]]:
    """Comparable operational metrics per department.

    Departments below :data:`MIN_TERMINAL_SAMPLE` terminal appointments are
    flagged ``rankable = False`` and carry a reason. The API still returns their
    figures — hiding them would make the dashboard lie by omission — but the UI
    must not rank them, and the ranking endpoint excludes them.
    """
    where, params = filters.appointment_predicates("a")
    appt_rows = conn.execute(
        f"""
        SELECT
            a.department_id,
            SUM(a.appointment_count),
            SUM(a.terminal_count),
            SUM(a.completed_count),
            SUM(a.cancelled_count),
            SUM(a.no_show_count),
            SUM(a.lead_time_valid_count),
            SUM(a.lead_time_sum_days)
        FROM analytics.mart_appointments_daily AS a
        WHERE {where}
        GROUP BY 1
        """,
        params,
    ).fetchall()

    where_e, params_e = filters.encounter_predicates("e")
    enc_rows = conn.execute(
        f"""
        SELECT
            e.department_id,
            SUM(e.encounter_count),
            SUM(e.completed_visit_count),
            SUM(e.wait_valid_count),
            SUM(e.wait_sum_minutes),
            SUM(e.wait_invalid_count),
            SUM(e.duration_valid_count),
            SUM(e.duration_sum_minutes)
        FROM analytics.mart_encounters_daily AS e
        WHERE {where_e}
        GROUP BY 1
        """,
        params_e,
    ).fetchall()

    # Median wait per department — from the fact table, one scan.
    where_f, params_f = filters.fact_encounter_predicates("f")
    median_rows = conn.execute(
        f"""
        SELECT
            f.department_id,
            MEDIAN(f.wait_minutes),
            COUNT(*) FILTER (WHERE f.wait_minutes IS NOT NULL)
        FROM core.fct_encounters AS f
        WHERE {where_f}
        GROUP BY 1
        """,
        params_f,
    ).fetchall()

    where_c, params_c = filters.encounter_predicates("c")
    cap_rows = conn.execute(
        f"""
        SELECT
            c.department_id,
            SUM(c.occupied_slots_valid),
            SUM(c.available_slots_valid),
            SUM(c.utilisation_valid_days)
        FROM analytics.mart_capacity_daily AS c
        WHERE {where_c}
        GROUP BY 1
        """,
        params_c,
    ).fetchall()

    dept_names = {
        row[0]: (row[1], row[2])
        for row in conn.execute(
            "SELECT department_id, department_name, specialty_group FROM core.dim_department"
        ).fetchall()
    }

    # Optional comparison window, keyed by department.
    comparison: dict[str, dict[str, float]] = {}
    if include_comparison:
        c_where, c_params = filters.appointment_predicates(
            "a", start=filters.comparison_start_date, end=filters.comparison_end_date
        )
        for row in conn.execute(
            f"""
            SELECT a.department_id, SUM(a.terminal_count), SUM(a.cancelled_count),
                   SUM(a.no_show_count), SUM(a.appointment_count)
            FROM analytics.mart_appointments_daily AS a
            WHERE {c_where}
            GROUP BY 1
            """,
            c_params,
        ).fetchall():
            comparison[row[0]] = {
                "terminal": row[1] or 0,
                "cancelled": row[2] or 0,
                "no_show": row[3] or 0,
                "appointments": row[4] or 0,
            }

        c_where_e, c_params_e = filters.encounter_predicates(
            "e", start=filters.comparison_start_date, end=filters.comparison_end_date
        )
        for row in conn.execute(
            f"""
            SELECT e.department_id, SUM(e.wait_valid_count), SUM(e.wait_sum_minutes)
            FROM analytics.mart_encounters_daily AS e
            WHERE {c_where_e}
            GROUP BY 1
            """,
            c_params_e,
        ).fetchall():
            entry = comparison.setdefault(row[0], {"terminal": 0, "cancelled": 0, "no_show": 0, "appointments": 0})
            entry["wait_valid"] = row[1] or 0
            entry["wait_sum"] = row[2] or 0.0

    enc_by_dept = {row[0]: row for row in enc_rows}
    median_by_dept = {row[0]: row for row in median_rows}
    cap_by_dept = {row[0]: row for row in cap_rows}

    results: list[dict[str, Any]] = []
    for row in appt_rows:
        dept_id = row[0]
        appointments, terminal, completed, cancelled, no_show = (
            int(row[1]), int(row[2]), int(row[3]), int(row[4]), int(row[5])
        )
        lead_valid, lead_sum = int(row[6]), float(row[7])

        enc = enc_by_dept.get(dept_id)
        encounters = int(enc[1]) if enc else 0
        completed_visits = int(enc[2]) if enc else 0
        wait_valid = int(enc[3]) if enc else 0
        wait_sum = float(enc[4]) if enc else 0.0
        wait_invalid = int(enc[5]) if enc else 0
        duration_valid = int(enc[6]) if enc else 0
        duration_sum = float(enc[7]) if enc else 0.0

        med = median_by_dept.get(dept_id)
        median_wait = float(med[1]) if med and med[1] is not None else None

        cap = cap_by_dept.get(dept_id)
        utilisation = _safe_rate(cap[1], cap[2]) if cap else None
        capacity_days = int(cap[3]) if cap else 0

        cancel_rate = _safe_rate(cancelled, terminal)
        no_show_rate = _safe_rate(no_show, terminal)
        mean_wait = (wait_sum / wait_valid) if wait_valid else None
        mean_lead = (lead_sum / lead_valid) if lead_valid else None

        # --- Period-over-period comparison ---------------------------------
        comp = comparison.get(dept_id, {})
        comp_terminal = int(comp.get("terminal", 0))
        comp_cancel_rate = _safe_rate(comp.get("cancelled"), comp_terminal)
        comp_no_show_rate = _safe_rate(comp.get("no_show"), comp_terminal)
        comp_wait_valid = int(comp.get("wait_valid", 0))
        comp_mean_wait = (comp["wait_sum"] / comp_wait_valid) if comp_wait_valid else None

        cancel_change, cancel_change_reason = period_over_period_change(cancel_rate, comp_cancel_rate)
        no_show_change, no_show_change_reason = period_over_period_change(no_show_rate, comp_no_show_rate)
        wait_change, wait_change_reason = period_over_period_change(mean_wait, comp_mean_wait)

        rankable = terminal >= MIN_TERMINAL_SAMPLE
        name, specialty = dept_names.get(dept_id, (dept_id, "Unassigned"))

        results.append(
            {
                "department_id": dept_id,
                "department_name": name,
                "specialty_group": specialty,
                "appointments_scheduled": appointments,
                "appointments_terminal": terminal,
                "appointments_completed": completed,
                "cancelled": cancelled,
                "no_show": no_show,
                "cancellation_rate": cancel_rate,
                "no_show_rate": no_show_rate,
                "lost_slot_rate": _safe_rate(cancelled + no_show, terminal),
                "encounters": encounters,
                "completed_visits": completed_visits,
                "median_wait_minutes": median_wait,
                "mean_wait_minutes": mean_wait,
                "wait_valid_count": wait_valid,
                "wait_invalid_count": wait_invalid,
                "mean_visit_duration_minutes": (duration_sum / duration_valid) if duration_valid else None,
                "appointment_lead_time_days": mean_lead,
                "capacity_utilisation": utilisation,
                "capacity_days_observed": capacity_days,
                "capacity_available": capacity_days > 0 and utilisation is not None,
                "rankable": rankable,
                "rankable_reason": (
                    None if rankable
                    else (
                        f"Only {terminal:,} terminal appointment(s) in this period; "
                        f"a rate needs at least {MIN_TERMINAL_SAMPLE} to be meaningful."
                    )
                ),
                "comparison": {
                    "cancellation_rate": comp_cancel_rate,
                    "cancellation_rate_change_percent": cancel_change,
                    "cancellation_rate_change_reason": cancel_change_reason,
                    "no_show_rate": comp_no_show_rate,
                    "no_show_rate_change_percent": no_show_change,
                    "no_show_rate_change_reason": no_show_change_reason,
                    "mean_wait_minutes": comp_mean_wait,
                    "mean_wait_change_percent": wait_change,
                    "mean_wait_change_reason": wait_change_reason,
                },
            }
        )

    results.sort(key=lambda item: (-(item["appointments_scheduled"] or 0), item["department_name"]))
    return results


# ---------------------------------------------------------------------------
# Data quality
# ---------------------------------------------------------------------------

def quality_runs(conn: duckdb.DuckDBPyConnection, limit: int = 10) -> list[dict[str, Any]]:
    """Recent pipeline runs, newest first."""
    try:
        rows = conn.execute(
            """
            SELECT run_id, started_at, completed_at, status,
                   input_rows, accepted_rows, rejected_rows, error_summary
            FROM quality.data_quality_runs
            ORDER BY completed_at DESC
            LIMIT ?
            """,
            [limit],
        ).fetchall()
    except duckdb.Error:
        return []

    runs = []
    for row in rows:
        try:
            summary = json.loads(row[7]) if row[7] else {}
        except json.JSONDecodeError:  # pragma: no cover - defensive
            summary = {}
        duration = None
        if isinstance(row[1], datetime) and isinstance(row[2], datetime):
            duration = round((row[2] - row[1]).total_seconds(), 2)
        runs.append(
            {
                "run_id": row[0],
                "started_at": row[1],
                "completed_at": row[2],
                "status": row[3],
                "input_rows": row[4],
                "accepted_rows": row[5],
                "rejected_rows": row[6],
                "duration_seconds": duration,
                "error_summary": summary,
            }
        )
    return runs


def quality_issues(conn: duckdb.DuckDBPyConnection, run_id: str | None = None) -> list[dict[str, Any]]:
    """Findings for a run (defaults to the latest), grouped by category."""
    try:
        if run_id is None:
            latest = conn.execute(
                "SELECT run_id FROM quality.data_quality_runs ORDER BY completed_at DESC LIMIT 1"
            ).fetchone()
            if latest is None:
                return []
            run_id = latest[0]

        rows = conn.execute(
            """
            SELECT check_id, category, severity, table_name, column_name,
                   row_count, message, remediation, sample_keys
            FROM quality.data_quality_issues
            WHERE run_id = ?
            ORDER BY
                CASE severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                row_count DESC
            """,
            [run_id],
        ).fetchall()
    except duckdb.Error:
        return []

    issues = []
    for row in rows:
        try:
            samples = json.loads(row[8]) if row[8] else []
        except json.JSONDecodeError:  # pragma: no cover - defensive
            samples = []
        issues.append(
            {
                "check_id": row[0],
                "category": row[1],
                "severity": row[2],
                "table": row[3],
                "column": row[4],
                "row_count": row[5],
                "message": row[6],
                "remediation": row[7],
                "sample_keys": samples,
            }
        )
    return issues


def quality_issue_categories(conn: duckdb.DuckDBPyConnection, run_id: str | None = None) -> list[dict[str, Any]]:
    """Issue counts by category and severity, for the Data Quality summary."""
    try:
        if run_id is None:
            latest = conn.execute(
                "SELECT run_id FROM quality.data_quality_runs ORDER BY completed_at DESC LIMIT 1"
            ).fetchone()
            if latest is None:
                return []
            run_id = latest[0]

        rows = conn.execute(
            """
            SELECT category, severity, COUNT(*), SUM(row_count)
            FROM quality.data_quality_issues
            WHERE run_id = ?
            GROUP BY 1, 2
            ORDER BY
                CASE severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                SUM(row_count) DESC
            """,
            [run_id],
        ).fetchall()
    except duckdb.Error:
        return []

    return [
        {
            "category": row[0],
            "severity": row[1],
            "check_count": int(row[2]),
            "affected_rows": int(row[3] or 0),
        }
        for row in rows
    ]


def table_row_counts(conn: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    """Row counts per analytical table, so the UI can show what is loaded."""
    tables = [
        ("core.dim_department", "Departments"),
        ("core.fct_appointments", "Appointments"),
        ("core.fct_encounters", "Encounters"),
        ("core.capacity_daily", "Capacity days"),
        ("analytics.mart_appointments_daily", "Appointment daily mart"),
        ("analytics.mart_encounters_daily", "Encounter daily mart"),
        ("analytics.mart_flow_distribution_daily", "Flow distribution mart"),
        ("analytics.mart_capacity_daily", "Capacity daily mart"),
    ]
    counts = []
    for relation, label in tables:
        try:
            count = conn.execute(f"SELECT COUNT(*) FROM {relation}").fetchone()[0]
        except duckdb.Error:  # pragma: no cover - defensive
            count = None
        counts.append({"relation": relation, "label": label, "row_count": count})
    return counts

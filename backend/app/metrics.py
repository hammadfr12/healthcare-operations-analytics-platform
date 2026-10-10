"""KPI computation.

Every metric in the PRD's KPI dictionary is computed here, once. The API, the
CSV exports and the Tableau extracts all read from these functions, which is
what makes "metric definitions match across the web app and Tableau" (PRD §12)
a structural property rather than a promise.

Three rules are enforced throughout:

1. **Zero denominators return ``None``, never 0, NaN or Infinity.** The UI
   renders ``None`` as "N/A". A rate of "0%" and a rate of "undefined" are
   different statements and must not be conflated.
2. **Rate metrics are recomputed from summed numerators and denominators**, not
   averaged from daily rates. Averaging daily rates would weight a quiet Sunday
   equally with a busy Monday.
3. **Sample size travels with every aggregate.** A mean wait with n=3 is not
   the same claim as a mean wait with n=3,000, and the UI is required to say so.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

import duckdb

from app.filters import FilterSet

logger = logging.getLogger(__name__)

# Rate metrics whose denominator is the count of *terminal* appointments.
TERMINAL_DENOMINATOR_NOTE = (
    "Denominator = appointments in a terminal state (completed + cancelled + "
    "no_show) in the selected period. Open/upcoming appointments are excluded."
)

# Below this many terminal appointments a rate is not published for a
# department: a 0% or 100% no-show rate computed from four appointments is
# noise, and ranking on it would mislead. See docs/KPI_DICTIONARY.md.
MIN_TERMINAL_SAMPLE: int = 30
MIN_WAIT_SAMPLE: int = 20


# ---------------------------------------------------------------------------
# Value containers
# ---------------------------------------------------------------------------

@dataclass
class Metric:
    """A single KPI value with the context needed to read it honestly."""

    key: str
    label: str
    value: float | None
    unit: Literal["count", "percent", "minutes", "days"]
    numerator: float | None = None
    denominator: float | None = None
    sample_size: int | None = None
    available: bool = True
    unavailable_reason: str | None = None
    definition: str = ""
    caveat: str | None = None
    # Period-over-period comparison, when one was requested.
    comparison_value: float | None = None
    change_percent: float | None = None
    change_available: bool = True
    change_unavailable_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "unit": self.unit,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "sample_size": self.sample_size,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "definition": self.definition,
            "caveat": self.caveat,
            "comparison_value": self.comparison_value,
            "change_percent": self.change_percent,
            "change_available": self.change_available,
            "change_unavailable_reason": self.change_unavailable_reason,
        }


def _safe_rate(numerator: float | None, denominator: float | None) -> float | None:
    """Return ``numerator / denominator * 100`` or ``None`` for an unusable denominator."""
    if numerator is None or denominator is None:
        return None
    if denominator <= 0:
        return None
    return (numerator / denominator) * 100.0


def period_over_period_change(
    current: float | None, comparison: float | None
) -> tuple[float | None, str | None]:
    """Compute ``(current - comparison) / comparison * 100``.

    Returns ``(None, reason)`` rather than raising or returning Infinity when the
    baseline is zero or either side is unavailable — the PRD requires an explicit
    N/A instead of a misleading infinite growth figure.
    """
    if current is None or comparison is None:
        return None, "One or both periods have no defined value for this metric."
    if comparison == 0:
        return None, "Comparison period baseline is zero, so a percentage change is undefined."
    return ((current - comparison) / abs(comparison)) * 100.0, None


# ---------------------------------------------------------------------------
# Aggregation primitives
# ---------------------------------------------------------------------------

@dataclass
class AppointmentAggregate:
    """Sums pulled from ``analytics.mart_appointments_daily``."""

    appointments: int = 0
    terminal: int = 0
    completed: int = 0
    cancelled: int = 0
    no_show: int = 0
    open: int = 0
    lead_time_valid_count: int = 0
    lead_time_sum_days: float = 0.0


@dataclass
class EncounterAggregate:
    """Sums pulled from ``analytics.mart_encounters_daily``."""

    encounters: int = 0
    completed_visits: int = 0
    walk_ins: int = 0
    wait_valid_count: int = 0
    wait_sum_minutes: float = 0.0
    wait_invalid_count: int = 0
    wait_negative_count: int = 0
    wait_implausible_count: int = 0
    duration_valid_count: int = 0
    duration_sum_minutes: float = 0.0
    duration_outlier_count: int = 0


@dataclass
class CapacityAggregate:
    """Sums pulled from ``analytics.mart_capacity_daily``."""

    available_slots: int = 0
    staffed_slots: int = 0
    occupied_slots: int = 0
    utilisation_valid_days: int = 0
    occupied_slots_valid: int = 0
    available_slots_valid: int = 0

    @property
    def has_data(self) -> bool:
        return self.utilisation_valid_days > 0 and self.available_slots_valid > 0


def _row_to_dataclass(row: tuple[Any, ...] | None, cls: type) -> Any:
    """Map a positional result row onto a dataclass, defaulting to zero."""
    instance = cls()
    if row is None:
        return instance
    for field_name, value in zip(cls.__dataclass_fields__, row):  # type: ignore[attr-defined]
        setattr(instance, field_name, value if value is not None else 0)
    return instance


def aggregate_appointments(
    conn: duckdb.DuckDBPyConnection,
    filters: FilterSet,
    *,
    start: Any = None,
    end: Any = None,
) -> AppointmentAggregate:
    """Sum the appointment mart over a window (defaults to the filter window)."""
    where, params = filters.appointment_predicates("m", start=start, end=end)
    row = conn.execute(
        f"""
        SELECT
            COALESCE(SUM(m.appointment_count), 0),
            COALESCE(SUM(m.terminal_count), 0),
            COALESCE(SUM(m.completed_count), 0),
            COALESCE(SUM(m.cancelled_count), 0),
            COALESCE(SUM(m.no_show_count), 0),
            COALESCE(SUM(m.open_count), 0),
            COALESCE(SUM(m.lead_time_valid_count), 0),
            COALESCE(SUM(m.lead_time_sum_days), 0.0)
        FROM analytics.mart_appointments_daily AS m
        WHERE {where}
        """,
        params,
    ).fetchone()
    return _row_to_dataclass(row, AppointmentAggregate)


def aggregate_encounters(
    conn: duckdb.DuckDBPyConnection,
    filters: FilterSet,
    *,
    start: Any = None,
    end: Any = None,
) -> EncounterAggregate:
    """Sum the encounter mart over a window."""
    where, params = filters.encounter_predicates("m", start=start, end=end)
    row = conn.execute(
        f"""
        SELECT
            COALESCE(SUM(m.encounter_count), 0),
            COALESCE(SUM(m.completed_visit_count), 0),
            COALESCE(SUM(m.walk_in_count), 0),
            COALESCE(SUM(m.wait_valid_count), 0),
            COALESCE(SUM(m.wait_sum_minutes), 0.0),
            COALESCE(SUM(m.wait_invalid_count), 0),
            COALESCE(SUM(m.wait_negative_count), 0),
            COALESCE(SUM(m.wait_implausible_count), 0),
            COALESCE(SUM(m.duration_valid_count), 0),
            COALESCE(SUM(m.duration_sum_minutes), 0.0),
            COALESCE(SUM(m.duration_outlier_count), 0)
        FROM analytics.mart_encounters_daily AS m
        WHERE {where}
        """,
        params,
    ).fetchone()
    return _row_to_dataclass(row, EncounterAggregate)


def aggregate_capacity(
    conn: duckdb.DuckDBPyConnection,
    filters: FilterSet,
    *,
    start: Any = None,
    end: Any = None,
) -> CapacityAggregate:
    """Sum the capacity mart over a window."""
    where, params = filters.encounter_predicates("m", start=start, end=end)
    row = conn.execute(
        f"""
        SELECT
            COALESCE(SUM(m.available_slots), 0),
            COALESCE(SUM(m.staffed_slots), 0),
            COALESCE(SUM(m.occupied_slots), 0),
            COALESCE(SUM(m.utilisation_valid_days), 0),
            COALESCE(SUM(m.occupied_slots_valid), 0),
            COALESCE(SUM(m.available_slots_valid), 0)
        FROM analytics.mart_capacity_daily AS m
        WHERE {where}
        """,
        params,
    ).fetchone()
    return _row_to_dataclass(row, CapacityAggregate)


def median_wait_minutes(conn: duckdb.DuckDBPyConnection, filters: FilterSet) -> tuple[float | None, int]:
    """Median of valid wait durations, plus the valid sample size.

    Computed from the fact table because a median cannot be recovered from daily
    sums. Invalid waits (negative or implausible) are excluded by the fact
    model itself, so they cannot pull the median.
    """
    where, params = filters.fact_encounter_predicates("e")
    row = conn.execute(
        f"""
        SELECT
            MEDIAN(e.wait_minutes),
            COUNT(*) FILTER (WHERE e.wait_minutes IS NOT NULL)
        FROM core.fct_encounters AS e
        WHERE {where}
        """,
        params,
    ).fetchone()
    if row is None or row[1] is None or row[1] == 0:
        return None, 0
    return (float(row[0]) if row[0] is not None else None), int(row[1])


def percentile_wait_minutes(
    conn: duckdb.DuckDBPyConnection, filters: FilterSet, percentiles: tuple[float, ...] = (0.5, 0.9, 0.95)
) -> dict[str, float | None]:
    """Percentile waits (p50/p90/p95) for the flow view."""
    where, params = filters.fact_encounter_predicates("e")
    select_parts = []
    for p in percentiles:
        alias = f"p{int(p * 100)}"
        select_parts.append(f"QUANTILE_CONT(e.wait_minutes, {p}) AS {alias}")
    row = conn.execute(
        f"""
        SELECT {", ".join(select_parts)}
        FROM core.fct_encounters AS e
        WHERE {where} AND e.wait_minutes IS NOT NULL
        """,
        params,
    ).fetchone()
    if row is None:
        return {f"p{int(p * 100)}": None for p in percentiles}
    return {
        f"p{int(p * 100)}": (float(value) if value is not None else None)
        for p, value in zip(percentiles, row)
    }


def median_lead_time_days(conn: duckdb.DuckDBPyConnection, filters: FilterSet) -> tuple[float | None, int]:
    """Median appointment lead time in days, plus the valid sample size."""
    where, params = filters.fact_appointment_predicates("a")
    row = conn.execute(
        f"""
        SELECT
            MEDIAN(a.lead_time_days),
            COUNT(*) FILTER (WHERE a.lead_time_days IS NOT NULL)
        FROM core.fct_appointments AS a
        WHERE {where}
        """,
        params,
    ).fetchone()
    if row is None or row[1] is None or row[1] == 0:
        return None, 0
    return (float(row[0]) if row[0] is not None else None), int(row[1])


# ---------------------------------------------------------------------------
# KPI assembly
# ---------------------------------------------------------------------------

def build_kpi_set(
    conn: duckdb.DuckDBPyConnection,
    filters: FilterSet,
    *,
    include_comparison: bool = True,
) -> dict[str, Metric]:
    """Compute the full KPI set for a window, with period-over-period change.

    Returns a mapping of metric key -> :class:`Metric`.
    """
    current_appt = aggregate_appointments(conn, filters)
    current_enc = aggregate_encounters(conn, filters)
    current_cap = aggregate_capacity(conn, filters)
    current_median_wait, current_wait_n = median_wait_minutes(conn, filters)

    comparison_appt = comparison_enc = comparison_cap = None
    comparison_median_wait: float | None = None
    if include_comparison:
        comparison_appt = aggregate_appointments(
            conn, filters,
            start=filters.comparison_start_date, end=filters.comparison_end_date,
        )
        comparison_enc = aggregate_encounters(
            conn, filters,
            start=filters.comparison_start_date, end=filters.comparison_end_date,
        )
        comparison_cap = aggregate_capacity(
            conn, filters,
            start=filters.comparison_start_date, end=filters.comparison_end_date,
        )
        comparison_median_wait, _ = median_wait_minutes(
            conn,
            FilterSet(
                start_date=filters.comparison_start_date,
                end_date=filters.comparison_end_date,
                department_ids=filters.department_ids,
                visit_types=filters.visit_types,
                statuses=filters.statuses,
                facility_ids=filters.facility_ids,
            ),
        )

    metrics: dict[str, Metric] = {}

    def add(
        key: str,
        label: str,
        value: float | None,
        unit: Literal["count", "percent", "minutes", "days"],
        *,
        definition: str,
        caveat: str | None = None,
        numerator: float | None = None,
        denominator: float | None = None,
        sample_size: int | None = None,
        comparison_value: float | None = None,
        available: bool = True,
        unavailable_reason: str | None = None,
    ) -> None:
        change, change_reason = (None, None)
        if include_comparison:
            change, change_reason = period_over_period_change(value, comparison_value)
        metrics[key] = Metric(
            key=key,
            label=label,
            value=value,
            unit=unit,
            numerator=numerator,
            denominator=denominator,
            sample_size=sample_size,
            available=available,
            unavailable_reason=unavailable_reason,
            definition=definition,
            caveat=caveat,
            comparison_value=comparison_value,
            change_percent=change,
            change_available=change is not None,
            change_unavailable_reason=change_reason,
        )

    # --- FR-04: appointments scheduled --------------------------------------
    add(
        "appointments_scheduled",
        "Appointments scheduled",
        float(current_appt.appointments),
        "count",
        definition=(
            "Distinct appointments in the selected period, counted on "
            "scheduled_start_at (the scheduled date basis)."
        ),
        caveat="Counted on the scheduled date, not the booking date.",
        comparison_value=float(comparison_appt.appointments) if comparison_appt else None,
    )

    # --- FR-04: completed visits -------------------------------------------
    add(
        "completed_visits",
        "Completed visits",
        float(current_enc.completed_visits),
        "count",
        definition="Distinct encounters with a completed encounter status.",
        caveat=(
            "Encounters, not appointments: a walk-in has no appointment, so this "
            "figure is not comparable with the appointment count above."
        ),
        comparison_value=float(comparison_enc.completed_visits) if comparison_enc else None,
    )

    # --- FR-04: cancellation rate ------------------------------------------
    cancel_rate = _safe_rate(current_appt.cancelled, current_appt.terminal)
    comparison_cancel_rate = (
        _safe_rate(comparison_appt.cancelled, comparison_appt.terminal) if comparison_appt else None
    )
    add(
        "cancellation_rate",
        "Cancellation rate",
        cancel_rate,
        "percent",
        definition="Cancelled appointments ÷ terminal appointments × 100.",
        caveat=(
            TERMINAL_DENOMINATOR_NOTE
            + " Patient-, provider- and facility-initiated cancellations are "
            "combined; the reason breakdown is on the Appointments page."
        ),
        numerator=float(current_appt.cancelled),
        denominator=float(current_appt.terminal),
        sample_size=current_appt.terminal,
        comparison_value=comparison_cancel_rate,
        available=current_appt.terminal > 0,
        unavailable_reason=(
            "No terminal appointments in the selected period."
            if current_appt.terminal == 0 else None
        ),
    )

    # --- FR-04: no-show rate ------------------------------------------------
    no_show_rate = _safe_rate(current_appt.no_show, current_appt.terminal)
    comparison_no_show_rate = (
        _safe_rate(comparison_appt.no_show, comparison_appt.terminal) if comparison_appt else None
    )
    add(
        "no_show_rate",
        "No-show rate",
        no_show_rate,
        "percent",
        definition="No-show appointments ÷ terminal appointments × 100.",
        caveat=TERMINAL_DENOMINATOR_NOTE,
        numerator=float(current_appt.no_show),
        denominator=float(current_appt.terminal),
        sample_size=current_appt.terminal,
        comparison_value=comparison_no_show_rate,
        available=current_appt.terminal > 0,
        unavailable_reason=(
            "No terminal appointments in the selected period."
            if current_appt.terminal == 0 else None
        ),
    )

    # --- FR-04: median wait -------------------------------------------------
    add(
        "median_wait_minutes",
        "Median wait",
        current_median_wait,
        "minutes",
        definition=(
            "Median of (service_start_at − check_in_at) over valid waits, in minutes."
        ),
        caveat=(
            "Negative and implausibly long waits (>24h) are excluded before the "
            "median is taken."
        ),
        sample_size=current_wait_n,
        comparison_value=comparison_median_wait,
        available=current_median_wait is not None,
        unavailable_reason=(
            "No valid wait durations in the selected period."
            if current_median_wait is None else None
        ),
    )

    # --- Average wait (paired with the median, per the KPI dictionary) ------
    mean_wait = (
        current_enc.wait_sum_minutes / current_enc.wait_valid_count
        if current_enc.wait_valid_count > 0 else None
    )
    comparison_mean_wait = (
        comparison_enc.wait_sum_minutes / comparison_enc.wait_valid_count
        if comparison_enc and comparison_enc.wait_valid_count > 0 else None
    )
    add(
        "average_wait_minutes",
        "Average wait",
        mean_wait,
        "minutes",
        definition="Mean of valid wait durations, in minutes.",
        caveat=(
            "Can be skewed by outliers; read alongside the median and the valid "
            "sample size."
        ),
        sample_size=current_enc.wait_valid_count,
        comparison_value=comparison_mean_wait,
        available=mean_wait is not None,
        unavailable_reason=(
            "No valid wait durations in the selected period." if mean_wait is None else None
        ),
    )

    # --- Average visit duration --------------------------------------------
    mean_duration = (
        current_enc.duration_sum_minutes / current_enc.duration_valid_count
        if current_enc.duration_valid_count > 0 else None
    )
    comparison_mean_duration = (
        comparison_enc.duration_sum_minutes / comparison_enc.duration_valid_count
        if comparison_enc and comparison_enc.duration_valid_count > 0 else None
    )
    add(
        "average_visit_duration_minutes",
        "Average visit duration",
        mean_duration,
        "minutes",
        definition="Mean of (service_end_at − service_start_at) over valid durations.",
        caveat=(
            f"{current_enc.duration_outlier_count:,} visit(s) exceeded 4 hours in "
            "this period and are included in the mean but flagged on the Patient "
            "Flow page."
        ),
        sample_size=current_enc.duration_valid_count,
        comparison_value=comparison_mean_duration,
        available=mean_duration is not None,
        unavailable_reason=(
            "No valid visit durations in the selected period." if mean_duration is None else None
        ),
    )

    # --- Appointment lead time ---------------------------------------------
    mean_lead_time = (
        current_appt.lead_time_sum_days / current_appt.lead_time_valid_count
        if current_appt.lead_time_valid_count > 0 else None
    )
    comparison_mean_lead = (
        comparison_appt.lead_time_sum_days / comparison_appt.lead_time_valid_count
        if comparison_appt and comparison_appt.lead_time_valid_count > 0 else None
    )
    add(
        "appointment_lead_time_days",
        "Appointment lead time",
        mean_lead_time,
        "days",
        definition="Mean of (scheduled_start_at − appointment_created_at), in days.",
        caveat="Requires a booking-created timestamp; appointments without one are excluded.",
        sample_size=current_appt.lead_time_valid_count,
        comparison_value=comparison_mean_lead,
        available=mean_lead_time is not None,
        unavailable_reason=(
            "No appointments with a usable booking timestamp in this period."
            if mean_lead_time is None else None
        ),
    )

    # --- Capacity utilisation (only when capacity data is present) ---------
    utilisation = _safe_rate(
        current_cap.occupied_slots_valid, current_cap.available_slots_valid
    )
    comparison_utilisation = (
        _safe_rate(comparison_cap.occupied_slots_valid, comparison_cap.available_slots_valid)
        if comparison_cap else None
    )
    add(
        "capacity_utilisation",
        "Capacity utilisation",
        utilisation,
        "percent",
        definition="Occupied slots ÷ available slots × 100, over days with usable capacity data.",
        caveat=(
            "Only published when reliable capacity/occupancy inputs are supplied. "
            "Days with missing capacity data are excluded from both sides of the ratio."
        ),
        numerator=float(current_cap.occupied_slots_valid),
        denominator=float(current_cap.available_slots_valid),
        sample_size=current_cap.utilisation_valid_days,
        comparison_value=comparison_utilisation,
        available=current_cap.has_data,
        unavailable_reason=(
            None if current_cap.has_data
            else "No usable capacity data was supplied, so utilisation is not defined."
        ),
    )

    # --- Derived: cancellation + no-show combined (useful for triage) ------
    lost_rate = _safe_rate(
        current_appt.cancelled + current_appt.no_show, current_appt.terminal
    )
    comparison_lost_rate = (
        _safe_rate(
            comparison_appt.cancelled + comparison_appt.no_show, comparison_appt.terminal
        )
        if comparison_appt else None
    )
    add(
        "lost_slot_rate",
        "Lost slot rate",
        lost_rate,
        "percent",
        definition="(Cancelled + no-show) ÷ terminal appointments × 100.",
        caveat=(
            "Convenience roll-up of the two rates above; the denominator is "
            "identical, so it never exceeds cancellation rate + no-show rate."
        ),
        numerator=float(current_appt.cancelled + current_appt.no_show),
        denominator=float(current_appt.terminal),
        sample_size=current_appt.terminal,
        comparison_value=comparison_lost_rate,
        available=current_appt.terminal > 0,
        unavailable_reason=(
            "No terminal appointments in the selected period."
            if current_appt.terminal == 0 else None
        ),
    )

    return metrics

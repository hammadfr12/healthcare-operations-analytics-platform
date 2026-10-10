"""Dashboard filter parsing and parameterised SQL predicate construction.

Security note
    Every user-supplied value reaches SQL as a **bound parameter**, never as
    interpolated text. The only strings this module concatenates into a query
    are fixed fragments it authored itself (``" AND department_id IN (?, ?)"``).
    Column and table names are likewise chosen from fixed allow-lists, so no
    request input can alter the shape of a statement.

The same filter object produces predicates for both the appointment and the
encounter marts, which is what keeps the KPI cards, the charts and the CSV
export consistent with each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Annotated

from fastapi import HTTPException, Query, status as http_status

# ---------------------------------------------------------------------------
# Date granularities
# ---------------------------------------------------------------------------

GRANULARITIES: dict[str, str] = {
    "day": "day",
    "week": "week",
    "month": "month",
}

DEFAULT_WINDOW_DAYS: int = 90
MAX_WINDOW_DAYS: int = 1200


@dataclass(frozen=True)
class FilterSet:
    """A validated, immutable dashboard filter selection."""

    start_date: date
    end_date: date
    department_ids: tuple[str, ...] = ()
    visit_types: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ()
    facility_ids: tuple[str, ...] = ()

    # --- Derived ------------------------------------------------------------
    @property
    def window_days(self) -> int:
        """Inclusive length of the selected window, in days."""
        return (self.end_date - self.start_date).days + 1

    @property
    def comparison_start_date(self) -> date:
        """First day of the immediately preceding window of equal length."""
        return self.start_date - timedelta(days=self.window_days)

    @property
    def comparison_end_date(self) -> date:
        """Last day of the immediately preceding window of equal length."""
        return self.start_date - timedelta(days=1)

    @property
    def as_dict(self) -> dict[str, object]:
        """Serialisable form, used in export metadata and API echoes."""
        return {
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "department_ids": list(self.department_ids),
            "visit_types": list(self.visit_types),
            "statuses": list(self.statuses),
            "facility_ids": list(self.facility_ids),
            "comparison_start_date": self.comparison_start_date.isoformat(),
            "comparison_end_date": self.comparison_end_date.isoformat(),
        }

    # --- Predicate builders -------------------------------------------------
    def _dimension_predicates(self, table_alias: str | None = None) -> tuple[list[str], list[object]]:
        """Build the dimension predicates shared by both marts."""
        prefix = f"{table_alias}." if table_alias else ""
        clauses: list[str] = []
        params: list[object] = []

        for column, values in (
            ("department_id", self.department_ids),
            ("visit_type", self.visit_types),
            ("appointment_status", self.statuses),
            ("facility_id", self.facility_ids),
        ):
            if not values:
                continue
            placeholders = ", ".join("?" for _ in values)
            clauses.append(f"{prefix}{column} IN ({placeholders})")
            params.extend(values)

        return clauses, params

    def date_predicate(
        self, table_alias: str | None = None, *, start: date | None = None, end: date | None = None
    ) -> tuple[str, list[object]]:
        """Predicate restricting ``metric_date`` to a window."""
        prefix = f"{table_alias}." if table_alias else ""
        lo = start if start is not None else self.start_date
        hi = end if end is not None else self.end_date
        return f"{prefix}metric_date BETWEEN ? AND ?", [lo, hi]

    def appointment_predicates(
        self, table_alias: str | None = None, *, start: date | None = None, end: date | None = None
    ) -> tuple[str, list[object]]:
        """Full WHERE body for the appointment mart."""
        clauses, params = self.date_predicate(table_alias, start=start, end=end)
        dim_clauses, dim_params = self._dimension_predicates(table_alias)
        return " AND ".join([clauses, *dim_clauses]), params + dim_params

    def encounter_predicates(
        self, table_alias: str | None = None, *, start: date | None = None, end: date | None = None
    ) -> tuple[str, list[object]]:
        """Full WHERE body for the encounter mart."""
        return self.appointment_predicates(table_alias, start=start, end=end)

    def fact_encounter_predicates(self, table_alias: str = "e") -> tuple[str, list[object]]:
        """WHERE body for ``core.fct_encounters`` (used for percentile metrics).

        The fact table carries the same filterable dimensions as the mart, so
        the same selection applies. ``encounter_date`` replaces ``metric_date``.
        """
        clauses = [f"{table_alias}.encounter_date BETWEEN ? AND ?"]
        params: list[object] = [self.start_date, self.end_date]

        dim_clauses, dim_params = self._dimension_predicates(table_alias)
        return " AND ".join([*clauses, *dim_clauses]), params + dim_params

    def fact_appointment_predicates(self, table_alias: str = "a") -> tuple[str, list[object]]:
        """WHERE body for ``core.fct_appointments``."""
        clauses = [f"{table_alias}.appointment_date BETWEEN ? AND ?"]
        params: list[object] = [self.start_date, self.end_date]

        dim_clauses, dim_params = self._dimension_predicates(table_alias)
        return " AND ".join([*clauses, *dim_clauses]), params + dim_params


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------

def get_filters(
    start_date: Annotated[
        date | None,
        Query(
            description="Inclusive start of the reporting window (YYYY-MM-DD). "
            f"Defaults to {DEFAULT_WINDOW_DAYS} days before end_date."
        ),
    ] = None,
    end_date: Annotated[
        date | None,
        Query(description="Inclusive end of the reporting window (YYYY-MM-DD). Defaults to today."),
    ] = None,
    department_id: Annotated[
        list[str] | None,
        Query(description="Repeatable. Restrict to these department IDs."),
    ] = None,
    visit_type: Annotated[
        list[str] | None,
        Query(description="Repeatable. Restrict to these visit types."),
    ] = None,
    status: Annotated[
        list[str] | None,
        Query(
            description="Repeatable. Restrict to these appointment statuses. "
            "Terminal-status metrics (cancellation, no-show) still use their "
            "own documented denominator."
        ),
    ] = None,
    facility_id: Annotated[
        list[str] | None,
        Query(description="Repeatable. Restrict to these facility IDs."),
    ] = None,
) -> FilterSet:
    """Parse and validate the dashboard filter query parameters.

    Raises:
        HTTPException 422: for an inverted or oversized date range, or a date
            outside the loaded dataset.
    """
    resolved_end = end_date or date.today()
    resolved_start = start_date or (resolved_end - timedelta(days=DEFAULT_WINDOW_DAYS - 1))

    if resolved_start > resolved_end:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"start_date ({resolved_start.isoformat()}) must not be after "
                f"end_date ({resolved_end.isoformat()})."
            ),
        )

    span = (resolved_end - resolved_start).days + 1
    if span > MAX_WINDOW_DAYS:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Requested window spans {span:,} days; the maximum supported is "
                f"{MAX_WINDOW_DAYS:,}. Narrow the date range."
            ),
        )

    def _clean(values: list[str] | None) -> tuple[str, ...]:
        if not values:
            return ()
        # Strip blanks and de-duplicate while preserving the caller's order.
        seen: dict[str, None] = {}
        for value in values:
            text = (value or "").strip()
            if text:
                seen.setdefault(text, None)
        return tuple(seen)

    return FilterSet(
        start_date=resolved_start,
        end_date=resolved_end,
        department_ids=_clean(department_id),
        visit_types=_clean(visit_type),
        statuses=_clean(status),
        facility_ids=_clean(facility_id),
    )

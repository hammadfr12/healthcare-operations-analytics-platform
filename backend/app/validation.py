"""Data validation and quality reporting for the ingestion pipeline.

Design rule (PRD §8, build spec "Never silently discard data"): validation
produces a *report*, not a filter. Every check returns the offending row keys
and a severity, and the pipeline decides what to quarantine. Nothing is dropped
without a corresponding issue being recorded against the run.

Severities
    error    the row cannot be used and is quarantined
    warning  the row is usable but something looks wrong; it is kept and flagged
    info     an observation worth surfacing (e.g. a legitimately open appointment)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Iterable

import pandas as pd

# ---------------------------------------------------------------------------
# Controlled vocabularies. These are the single source of truth for valid
# values; the SQL models and the UI legends both key off them.
# ---------------------------------------------------------------------------

APPOINTMENT_STATUSES: frozenset[str] = frozenset(
    {"scheduled", "completed", "cancelled", "no_show"}
)
ENCOUNTER_STATUSES: frozenset[str] = frozenset(
    {"completed", "in_progress", "left_without_being_seen", "cancelled"}
)
VISIT_TYPES: frozenset[str] = frozenset({"new", "follow_up", "procedure", "urgent"})
CANCELLATION_REASON_CATEGORIES: frozenset[str] = frozenset(
    {
        "patient_unavailable",
        "patient_rescheduled",
        "transport",
        "provider_unavailable",
        "schedule_change",
        "facility_closure",
        "equipment_unavailable",
    }
)

# Thresholds used by the duration checks. Chosen as generous operational
# bounds: anything outside them is far more likely to be a data error than a
# real event. They are constants so the API can publish them to the UI.
MAX_PLAUSIBLE_WAIT_MINUTES: float = 24 * 60
MAX_PLAUSIBLE_VISIT_MINUTES: float = 24 * 60
DURATION_OUTLIER_MINUTES: float = 4 * 60


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class Issue:
    """A single data-quality finding, aggregated over affected rows."""

    check_id: str
    category: str
    severity: Severity
    table: str
    column: str | None
    row_count: int
    message: str
    remediation: str
    sample_keys: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "category": self.category,
            "severity": self.severity.value,
            "table": self.table,
            "column": self.column,
            "row_count": self.row_count,
            "message": self.message,
            "remediation": self.remediation,
            "sample_keys": self.sample_keys,
        }


@dataclass
class ValidationResult:
    """Outcome of validating one table: accepted rows plus every issue found."""

    table: str
    input_rows: int
    accepted: pd.DataFrame
    issues: list[Issue]
    rejected: pd.DataFrame

    @property
    def accepted_rows(self) -> int:
        return int(len(self.accepted))

    @property
    def rejected_rows(self) -> int:
        return int(len(self.rejected))

    @property
    def error_issues(self) -> list[Issue]:
        return [i for i in self.issues if i.severity is Severity.ERROR]

    @property
    def warning_issues(self) -> list[Issue]:
        return [i for i in self.issues if i.severity is Severity.WARNING]

    @property
    def status(self) -> str:
        """``failed`` when any row was quarantined, else ``succeeded``."""
        return "failed" if self.rejected_rows > 0 else "succeeded"


# ---------------------------------------------------------------------------
# Check primitives
# ---------------------------------------------------------------------------

def _sample(values: Iterable[Any], limit: int = 5) -> list[str]:
    """Return up to ``limit`` distinct stringified sample values."""
    seen: list[str] = []
    for value in values:
        text = str(value)
        if text not in seen:
            seen.append(text)
        if len(seen) >= limit:
            break
    return seen


def _blank_mask(series: pd.Series) -> pd.Series:
    """True where a value is null, NaN, or whitespace-only."""
    if series.dtype == object:
        return series.isna() | (series.astype("string").str.strip() == "")
    return series.isna()


def check_required_columns(
    df: pd.DataFrame, table: str, required: list[str]
) -> list[Issue]:
    """Every declared column must be present, even if entirely null."""
    missing = [c for c in required if c not in df.columns]
    if not missing:
        return []
    return [
        Issue(
            check_id=f"{table}.missing_columns",
            category="schema",
            severity=Severity.ERROR,
            table=table,
            column=None,
            row_count=len(df),
            message=f"Required column(s) absent from the file: {', '.join(missing)}.",
            remediation=(
                "Add the missing column(s) to the source extract, or update the "
                "contract in docs/DATA_DICTIONARY.md if the field was intentionally "
                "removed."
            ),
        )
    ]


def check_required_values(
    df: pd.DataFrame, table: str, key: str, columns: list[str]
) -> tuple[list[Issue], pd.Series]:
    """Flag rows where a required column is null/blank.

    Returns the issues and a boolean mask of offending rows.
    """
    issues: list[Issue] = []
    mask = pd.Series(False, index=df.index)

    for column in columns:
        if column not in df.columns:
            continue
        bad = _blank_mask(df[column])
        if not bad.any():
            continue
        mask |= bad
        issues.append(
            Issue(
                check_id=f"{table}.{column}.missing_value",
                category="missing_value",
                severity=Severity.ERROR,
                table=table,
                column=column,
                row_count=int(bad.sum()),
                message=(
                    f"{int(bad.sum()):,} row(s) have no value in required column "
                    f"'{column}'."
                ),
                remediation=(
                    f"Populate '{column}' at source, or exclude the row from the "
                    "extract. Rows missing a required key are quarantined and "
                    "listed in the rejected-records output."
                ),
                sample_keys=_sample(df.loc[bad, key]) if key in df.columns else [],
            )
        )
    return issues, mask


def check_unique_key(
    df: pd.DataFrame, table: str, key: str
) -> tuple[list[Issue], pd.Series]:
    """Flag duplicate primary keys, keeping the first occurrence."""
    if key not in df.columns:
        return [], pd.Series(False, index=df.index)

    duplicated = df[key].duplicated(keep="first") & ~_blank_mask(df[key])
    if not duplicated.any():
        return [], duplicated

    dupes = df.loc[duplicated, key]
    return (
        [
            Issue(
                check_id=f"{table}.{key}.duplicate",
                category="duplicate_key",
                severity=Severity.ERROR,
                table=table,
                column=key,
                row_count=int(duplicated.sum()),
                message=(
                    f"{int(duplicated.sum()):,} duplicate '{key}' value(s); "
                    f"{dupes.nunique():,} distinct key(s) affected. The first "
                    "occurrence of each key is kept."
                ),
                remediation=(
                    "De-duplicate the source extract on its primary key. If the "
                    "duplicates are genuinely distinct events, the source needs a "
                    "composite or surrogate key instead."
                ),
                sample_keys=_sample(dupes),
            )
        ],
        duplicated,
    )


def check_timestamps(
    df: pd.DataFrame, table: str, key: str, columns: list[str]
) -> tuple[list[Issue], pd.Series, dict[str, pd.Series]]:
    """Parse timestamp columns; flag values that cannot be parsed.

    Returns issues, a combined bad-row mask, and the parsed columns so the
    pipeline can reuse the parse instead of repeating it.
    """
    issues: list[Issue] = []
    mask = pd.Series(False, index=df.index)
    parsed: dict[str, pd.Series] = {}

    for column in columns:
        if column not in df.columns:
            continue
        # Blank cells are a missing-value problem, handled by that check; only
        # non-blank unparseable text is a malformed-timestamp problem.
        blank = _blank_mask(df[column])
        as_dt = pd.to_datetime(df[column], errors="coerce", format="ISO8601")
        # Fall back to a permissive parse for non-ISO text before declaring failure.
        still_null = as_dt.isna() & ~blank
        if still_null.any():
            as_dt = as_dt.where(~still_null, pd.to_datetime(df[column], errors="coerce"))

        bad = as_dt.isna() & ~blank
        parsed[column] = as_dt
        if not bad.any():
            continue

        mask |= bad
        issues.append(
            Issue(
                check_id=f"{table}.{column}.malformed_timestamp",
                category="malformed_timestamp",
                severity=Severity.ERROR,
                table=table,
                column=column,
                row_count=int(bad.sum()),
                message=(
                    f"{int(bad.sum()):,} row(s) have an unparseable timestamp in "
                    f"'{column}'."
                ),
                remediation=(
                    f"Emit '{column}' as ISO-8601 (YYYY-MM-DDTHH:MM:SS) in the "
                    "facility timezone. Malformed rows are quarantined."
                ),
                sample_keys=_sample(df.loc[bad, key]) if key in df.columns else [],
            )
        )
    return issues, mask, parsed


def check_allowed_values(
    df: pd.DataFrame,
    table: str,
    key: str,
    column: str,
    allowed: frozenset[str],
    *,
    severity: Severity = Severity.ERROR,
) -> tuple[list[Issue], pd.Series]:
    """Flag values outside a controlled vocabulary."""
    if column not in df.columns:
        return [], pd.Series(False, index=df.index)

    present = ~_blank_mask(df[column])
    bad = present & ~df[column].astype("string").isin(allowed)
    if not bad.any():
        return [], bad

    found = sorted({str(v) for v in df.loc[bad, column].dropna()})[:5]
    return (
        [
            Issue(
                check_id=f"{table}.{column}.unknown_value",
                category="invalid_status" if "status" in column else "invalid_value",
                severity=severity,
                table=table,
                column=column,
                row_count=int(bad.sum()),
                message=(
                    f"{int(bad.sum()):,} row(s) have a '{column}' value outside the "
                    f"controlled vocabulary. Unrecognised value(s): "
                    f"{', '.join(repr(v) for v in found)}."
                ),
                remediation=(
                    f"Map the source value(s) to the documented '{column}' "
                    "vocabulary in docs/DATA_DICTIONARY.md, or extend the "
                    "vocabulary deliberately and update the KPI definitions."
                ),
                sample_keys=_sample(df.loc[bad, key]) if key in df.columns else [],
            )
        ],
        bad,
    )


def check_referential_integrity(
    df: pd.DataFrame,
    table: str,
    key: str,
    column: str,
    valid_values: set[str],
    parent_table: str,
) -> tuple[list[Issue], pd.Series]:
    """Flag foreign keys with no matching row in the parent dimension."""
    if column not in df.columns:
        return [], pd.Series(False, index=df.index)

    present = ~_blank_mask(df[column])
    bad = present & ~df[column].astype("string").isin(valid_values)
    if not bad.any():
        return [], bad

    orphans = sorted({str(v) for v in df.loc[bad, column].dropna()})[:5]
    return (
        [
            Issue(
                check_id=f"{table}.{column}.orphan_reference",
                category="referential_integrity",
                severity=Severity.ERROR,
                table=table,
                column=column,
                row_count=int(bad.sum()),
                message=(
                    f"{int(bad.sum()):,} row(s) reference a '{column}' that does not "
                    f"exist in {parent_table}. Unmatched value(s): "
                    f"{', '.join(repr(v) for v in orphans)}."
                ),
                remediation=(
                    f"Load the missing {parent_table} rows first, or correct the "
                    "reference at source. Orphan rows are quarantined so department "
                    "comparisons cannot silently drop them."
                ),
                sample_keys=_sample(df.loc[bad, key]) if key in df.columns else [],
            )
        ],
        bad,
    )


def check_duration_ordering(
    df: pd.DataFrame,
    table: str,
    key: str,
    start_col: str,
    end_col: str,
    parsed: dict[str, pd.Series],
    *,
    duration_label: str,
    max_minutes: float,
) -> tuple[list[Issue], pd.Series]:
    """Flag negative and implausibly long durations.

    Both are WARNINGS, not errors: the row is still usable for every other
    metric, so it is kept and excluded only from the duration aggregates. This
    is what lets the UI show "n valid" alongside a mean.
    """
    if start_col not in parsed or end_col not in parsed:
        return [], pd.Series(False, index=df.index)

    start, end = parsed[start_col], parsed[end_col]
    both = start.notna() & end.notna()

    negative = both & (end < start)
    too_long = both & ((end - start).dt.total_seconds() / 60.0 > max_minutes)
    flagged = negative | too_long

    issues: list[Issue] = []
    if negative.any():
        issues.append(
            Issue(
                check_id=f"{table}.{duration_label}.negative_duration",
                category="invalid_duration",
                severity=Severity.WARNING,
                table=table,
                column=f"{start_col},{end_col}",
                row_count=int(negative.sum()),
                message=(
                    f"{int(negative.sum()):,} row(s) have {end_col} earlier than "
                    f"{start_col}, giving a negative {duration_label}."
                ),
                remediation=(
                    f"Investigate the source capture order for {start_col}/{end_col}. "
                    f"These rows are kept but excluded from {duration_label} "
                    "aggregates and reported as an invalid count in the UI."
                ),
                sample_keys=_sample(df.loc[negative, key]) if key in df.columns else [],
            )
        )
    if too_long.any():
        issues.append(
            Issue(
                check_id=f"{table}.{duration_label}.implausible_duration",
                category="invalid_duration",
                severity=Severity.WARNING,
                table=table,
                column=f"{start_col},{end_col}",
                row_count=int(too_long.sum()),
                message=(
                    f"{int(too_long.sum()):,} row(s) have a {duration_label} longer "
                    f"than {int(max_minutes / 60)} hours."
                ),
                remediation=(
                    "Confirm whether these are genuine long-stay events or capture "
                    f"errors. They are kept but excluded from {duration_label} "
                    "aggregates; the excluded count is shown beside every mean."
                ),
                sample_keys=_sample(df.loc[too_long, key]) if key in df.columns else [],
            )
        )
    return issues, flagged


# ---------------------------------------------------------------------------
# Table validators
# ---------------------------------------------------------------------------

DEPARTMENTS_CONTRACT = ["department_id", "department_name", "specialty_group", "facility_id"]

APPOINTMENTS_CONTRACT = [
    "appointment_id",
    "facility_id",
    "department_id",
    "appointment_created_at",
    "scheduled_start_at",
    "status",
    "visit_type",
    "cancellation_reason_category",
]

ENCOUNTERS_CONTRACT = [
    "encounter_id",
    "appointment_id",
    "facility_id",
    "department_id",
    "check_in_at",
    "service_start_at",
    "service_end_at",
    "encounter_status",
]

CAPACITY_CONTRACT = [
    "date",
    "facility_id",
    "department_id",
    "available_slots",
    "staffed_slots",
    "occupied_slots",
]


def validate_departments(df: pd.DataFrame) -> ValidationResult:
    """Validate the department dimension."""
    schema_issues = check_required_columns(df, "departments", DEPARTMENTS_CONTRACT)
    if schema_issues:
        return ValidationResult("departments", len(df), df.iloc[0:0], schema_issues, df)

    issues: list[Issue] = []
    required_issues, missing = check_required_values(
        df, "departments", "department_id", ["department_id", "department_name"]
    )
    issues += required_issues
    dup_issues, dup = check_unique_key(df, "departments", "department_id")
    issues += dup_issues

    rejected_mask = missing | dup
    return ValidationResult(
        "departments",
        len(df),
        df.loc[~rejected_mask].copy(),
        issues,
        df.loc[rejected_mask].copy(),
    )


def validate_appointments(
    df: pd.DataFrame, valid_departments: set[str], valid_facilities: set[str]
) -> ValidationResult:
    """Validate the appointment fact and quarantine unusable rows."""
    schema_issues = check_required_columns(df, "appointments", APPOINTMENTS_CONTRACT)
    if schema_issues:
        return ValidationResult("appointments", len(df), df.iloc[0:0], schema_issues, df)

    issues: list[Issue] = []

    req_issues, missing = check_required_values(
        df,
        "appointments",
        "appointment_id",
        ["appointment_id", "department_id", "facility_id", "scheduled_start_at", "status"],
    )
    issues += req_issues

    dup_issues, dup = check_unique_key(df, "appointments", "appointment_id")
    issues += dup_issues

    ts_issues, bad_ts, parsed = check_timestamps(
        df,
        "appointments",
        "appointment_id",
        ["scheduled_start_at", "appointment_created_at"],
    )
    issues += ts_issues

    status_issues, bad_status = check_allowed_values(
        df, "appointments", "appointment_id", "status", APPOINTMENT_STATUSES
    )
    issues += status_issues

    visit_issues, bad_visit = check_allowed_values(
        df, "appointments", "appointment_id", "visit_type", VISIT_TYPES,
        severity=Severity.WARNING,
    )
    issues += visit_issues

    # Cancellation reason is only meaningful for cancelled appointments. A
    # populated reason on a non-cancelled row is a consistency warning, not an
    # error — the row's status still drives every rate.
    if "cancellation_reason_category" in df.columns:
        reason_present = ~_blank_mask(df["cancellation_reason_category"])
        mismatched = reason_present & (df["status"].astype("string") != "cancelled")
        if mismatched.any():
            issues.append(
                Issue(
                    check_id="appointments.cancellation_reason.unexpected_value",
                    category="consistency",
                    severity=Severity.WARNING,
                    table="appointments",
                    column="cancellation_reason_category",
                    row_count=int(mismatched.sum()),
                    message=(
                        f"{int(mismatched.sum()):,} row(s) carry a cancellation "
                        "reason but are not in a cancelled state."
                    ),
                    remediation=(
                        "Clear the reason for non-cancelled rows, or correct the "
                        "status. The row is kept; rates use status only."
                    ),
                    sample_keys=_sample(df.loc[mismatched, "appointment_id"]),
                )
            )

    dept_issues, bad_dept = check_referential_integrity(
        df, "appointments", "appointment_id", "department_id",
        valid_departments, "departments",
    )
    issues += dept_issues

    fac_issues, bad_fac = check_referential_integrity(
        df, "appointments", "appointment_id", "facility_id",
        valid_facilities, "facilities",
    )
    issues += fac_issues

    # Lead time ordering: an appointment cannot be scheduled before it was booked.
    lead_issues, bad_lead = check_duration_ordering(
        df, "appointments", "appointment_id",
        "appointment_created_at", "scheduled_start_at", parsed,
        duration_label="lead_time", max_minutes=365 * 24 * 60,
    )
    issues += lead_issues

    # Quarantine only ERROR-class problems. Duration/consistency warnings are
    # kept so their effect stays visible in the aggregates.
    rejected_mask = missing | dup | bad_ts | bad_status | bad_dept | bad_fac
    return ValidationResult(
        "appointments",
        len(df),
        df.loc[~rejected_mask].copy(),
        issues,
        df.loc[rejected_mask].copy(),
    )


def validate_encounters(
    df: pd.DataFrame, valid_departments: set[str], valid_facilities: set[str]
) -> ValidationResult:
    """Validate the patient-flow fact and quarantine unusable rows."""
    schema_issues = check_required_columns(df, "encounters", ENCOUNTERS_CONTRACT)
    if schema_issues:
        return ValidationResult("encounters", len(df), df.iloc[0:0], schema_issues, df)

    issues: list[Issue] = []

    req_issues, missing = check_required_values(
        df,
        "encounters",
        "encounter_id",
        ["encounter_id", "department_id", "facility_id", "check_in_at", "encounter_status"],
    )
    issues += req_issues

    dup_issues, dup = check_unique_key(df, "encounters", "encounter_id")
    issues += dup_issues

    ts_issues, bad_ts, parsed = check_timestamps(
        df,
        "encounters",
        "encounter_id",
        ["check_in_at", "service_start_at", "service_end_at"],
    )
    issues += ts_issues

    status_issues, bad_status = check_allowed_values(
        df, "encounters", "encounter_id", "encounter_status", ENCOUNTER_STATUSES
    )
    issues += status_issues

    dept_issues, bad_dept = check_referential_integrity(
        df, "encounters", "encounter_id", "department_id",
        valid_departments, "departments",
    )
    issues += dept_issues

    fac_issues, bad_fac = check_referential_integrity(
        df, "encounters", "encounter_id", "facility_id",
        valid_facilities, "facilities",
    )
    issues += fac_issues

    # Wait time and visit duration: ordering + plausibility. Warnings only.
    wait_issues, _ = check_duration_ordering(
        df, "encounters", "encounter_id", "check_in_at", "service_start_at", parsed,
        duration_label="wait_time", max_minutes=MAX_PLAUSIBLE_WAIT_MINUTES,
    )
    issues += wait_issues

    duration_issues, _ = check_duration_ordering(
        df, "encounters", "encounter_id", "service_start_at", "service_end_at", parsed,
        duration_label="visit_duration", max_minutes=MAX_PLAUSIBLE_VISIT_MINUTES,
    )
    issues += duration_issues

    rejected_mask = missing | dup | bad_ts | bad_status | bad_dept | bad_fac
    return ValidationResult(
        "encounters",
        len(df),
        df.loc[~rejected_mask].copy(),
        issues,
        df.loc[rejected_mask].copy(),
    )


def validate_capacity(
    df: pd.DataFrame, valid_departments: set[str], valid_facilities: set[str]
) -> ValidationResult:
    """Validate the optional capacity input.

    Capacity is optional by design. An empty frame is a valid outcome and
    yields no issues — the platform then reports utilisation as unavailable.
    """
    if df.empty:
        return ValidationResult("capacity_daily", 0, df, [], df)

    schema_issues = check_required_columns(df, "capacity_daily", CAPACITY_CONTRACT)
    if schema_issues:
        return ValidationResult("capacity_daily", len(df), df.iloc[0:0], schema_issues, df)

    issues: list[Issue] = []

    # Duplicate grain key: one row per date x facility x department.
    grain_key = (
        df["date"].astype("string")
        + "|" + df["facility_id"].astype("string")
        + "|" + df["department_id"].astype("string")
    )
    dup_mask = grain_key.duplicated(keep="first")
    if dup_mask.any():
        issues.append(
            Issue(
                check_id="capacity_daily.grain.duplicate",
                category="duplicate_key",
                severity=Severity.ERROR,
                table="capacity_daily",
                column="date,facility_id,department_id",
                row_count=int(dup_mask.sum()),
                message=(
                    f"{int(dup_mask.sum()):,} duplicate row(s) for the "
                    "date x facility x department grain. The first occurrence is kept."
                ),
                remediation=(
                    "Aggregate the source to one row per date x facility x "
                    "department before loading."
                ),
                sample_keys=_sample(grain_key[dup_mask]),
            )
        )

    # Numeric coercion; non-numeric capacity values are errors.
    numeric_cols = ["available_slots", "staffed_slots", "occupied_slots"]
    coerced: dict[str, pd.Series] = {}
    bad_numeric = pd.Series(False, index=df.index)
    for column in numeric_cols:
        values = pd.to_numeric(df[column], errors="coerce")
        blank = _blank_mask(df[column])
        bad = values.isna() & ~blank
        coerced[column] = values
        if bad.any():
            bad_numeric |= bad
            issues.append(
                Issue(
                    check_id=f"capacity_daily.{column}.not_numeric",
                    category="invalid_type",
                    severity=Severity.ERROR,
                    table="capacity_daily",
                    column=column,
                    row_count=int(bad.sum()),
                    message=f"{int(bad.sum()):,} row(s) have a non-numeric '{column}'.",
                    remediation=f"Emit '{column}' as an integer count.",
                    sample_keys=_sample(df.loc[bad, "date"]),
                )
            )

    # Negative capacity is impossible.
    negative = pd.Series(False, index=df.index)
    for column in numeric_cols:
        bad = coerced[column].notna() & (coerced[column] < 0)
        negative |= bad
    if negative.any():
        issues.append(
            Issue(
                check_id="capacity_daily.negative_capacity",
                category="invalid_value",
                severity=Severity.ERROR,
                table="capacity_daily",
                column="available_slots,staffed_slots,occupied_slots",
                row_count=int(negative.sum()),
                message=f"{int(negative.sum()):,} row(s) have a negative slot count.",
                remediation="Slot counts cannot be negative; correct the source extract.",
                sample_keys=_sample(df.loc[negative, "date"]),
            )
        )

    # Occupancy exceeding availability — a warning, since utilisation is capped.
    overflow = (
        coerced["occupied_slots"].notna()
        & coerced["available_slots"].notna()
        & (coerced["occupied_slots"] > coerced["available_slots"])
    )
    if overflow.any():
        issues.append(
            Issue(
                check_id="capacity_daily.occupancy.over_available",
                category="consistency",
                severity=Severity.WARNING,
                table="capacity_daily",
                column="occupied_slots,available_slots",
                row_count=int(overflow.sum()),
                message=(
                    f"{int(overflow.sum()):,} row(s) have more occupied slots than "
                    "available slots."
                ),
                remediation=(
                    "Utilisation is capped at 100% for these rows so a single bad "
                    "day cannot distort the KPI, but the source should be corrected."
                ),
                sample_keys=_sample(df.loc[overflow, "date"]),
            )
        )

    dept_issues, bad_dept = check_referential_integrity(
        df, "capacity_daily", "date", "department_id", valid_departments, "departments",
    )
    issues += dept_issues
    fac_issues, bad_fac = check_referential_integrity(
        df, "capacity_daily", "date", "facility_id", valid_facilities, "facilities",
    )
    issues += fac_issues

    rejected_mask = dup_mask | bad_numeric | negative | bad_dept | bad_fac
    return ValidationResult(
        "capacity_daily",
        len(df),
        df.loc[~rejected_mask].copy(),
        issues,
        df.loc[rejected_mask].copy(),
    )


def validate_dataset(
    tables: dict[str, pd.DataFrame]
) -> dict[str, ValidationResult]:
    """Validate every table, in dependency order.

    Departments are validated first so their accepted key set can seed the
    referential-integrity checks on the facts.
    """
    results: dict[str, ValidationResult] = {}

    results["departments"] = validate_departments(tables["departments"])
    valid_departments = set(
        results["departments"].accepted["department_id"].dropna().astype(str)
    )

    # Facilities are derived from the appointment/encounter extracts rather
    # than supplied separately, mirroring how the dimension is built in SQL.
    facility_frames = [
        tables[name]["facility_id"].dropna().astype(str)
        for name in ("appointments", "encounters", "capacity_daily")
        if name in tables and "facility_id" in tables[name].columns
    ]
    valid_facilities = set(pd.concat(facility_frames).unique()) if facility_frames else set()

    results["appointments"] = validate_appointments(
        tables["appointments"], valid_departments, valid_facilities
    )
    results["encounters"] = validate_encounters(
        tables["encounters"], valid_departments, valid_facilities
    )
    results["capacity_daily"] = validate_capacity(
        tables["capacity_daily"], valid_departments, valid_facilities
    )
    return results


def summarise_issues(results: dict[str, ValidationResult]) -> dict[str, Any]:
    """Build the machine-readable error summary persisted on the quality run."""
    all_issues = [issue for result in results.values() for issue in result.issues]
    by_severity: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for issue in all_issues:
        by_severity[issue.severity.value] = by_severity.get(issue.severity.value, 0) + 1
        by_category[issue.category] = by_category.get(issue.category, 0) + 1

    return {
        "issue_count": len(all_issues),
        "by_severity": by_severity,
        "by_category": by_category,
        "tables": {
            name: {
                "input_rows": result.input_rows,
                "accepted_rows": result.accepted_rows,
                "rejected_rows": result.rejected_rows,
                "status": result.status,
            }
            for name, result in results.items()
        },
    }

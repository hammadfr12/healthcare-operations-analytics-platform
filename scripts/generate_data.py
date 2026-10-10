#!/usr/bin/env python3
"""Generate the deterministic synthetic hospital operations dataset.

Every value produced here is FABRICATED for demonstration. No real patient,
staff member, department or facility is represented. The generator is fully
seeded, so the same seed always yields byte-identical CSVs and therefore
identical KPIs — which is what makes the reconciliation examples in
``docs/KPI_DICTIONARY.md`` checkable.

Outputs (written to ``data/raw/``):
    departments.csv     department_id, department_name, specialty_group
    appointments.csv    the appointment fact, one row per booking
    encounters.csv      the patient-flow fact, linked to appointments where one exists
    capacity_daily.csv  optional capacity/occupancy inputs

A deliberately corrupted fixture is written to ``data/raw/flawed/`` so the
validation layer can be demonstrated failing without touching the happy path.

Usage:
    python scripts/generate_data.py [--seed N] [--days N] [--anchor YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

# Make ``backend`` importable when this script is run directly.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.config import get_settings  # noqa: E402

# ---------------------------------------------------------------------------
# Reference data — synthetic departments and their operating characteristics.
#
# The per-department profiles below are plausible-looking OPERATIONAL choices
# made for demonstration. They are NOT clinically validated, and no result
# derived from them says anything about real hospital performance.
# ---------------------------------------------------------------------------

FACILITIES: tuple[str, ...] = ("FAC-01", "FAC-02")


@dataclass(frozen=True)
class DepartmentProfile:
    """Operating profile for one synthetic department."""

    department_id: str
    department_name: str
    specialty_group: str
    facility_id: str
    # Mean appointments booked per weekday.
    daily_volume: float
    # Terminal-outcome mix (must sum to <= 1.0; the remainder completes).
    cancel_rate: float
    no_show_rate: float
    # Fraction of cancellations attributed to the patient (vs provider/facility).
    patient_cancel_share: float
    # Wait time (check-in -> service start), minutes.
    wait_mean: float
    wait_sd: float
    # Face-to-face service duration, minutes.
    duration_mean: float
    duration_sd: float
    # Share of this department's encounters that are walk-ins (no appointment).
    walk_in_share: float
    # Average booking lead time in days.
    lead_time_mean: float
    lead_time_sd: float
    # Capacity: slots offered per weekday, and the mean occupancy fraction.
    slots_per_weekday: int
    occupancy_mean: float


DEPARTMENTS: tuple[DepartmentProfile, ...] = (
    DepartmentProfile(
        "DEPT-01", "Emergency", "Emergency & Acute", "FAC-01",
        daily_volume=38.0, cancel_rate=0.035, no_show_rate=0.045,
        patient_cancel_share=0.45,
        wait_mean=22.0, wait_sd=14.0, duration_mean=95.0, duration_sd=38.0,
        walk_in_share=0.62, lead_time_mean=0.6, lead_time_sd=0.8,
        slots_per_weekday=52, occupancy_mean=0.88,
    ),
    DepartmentProfile(
        "DEPT-02", "Cardiology", "Cardiovascular", "FAC-01",
        daily_volume=26.0, cancel_rate=0.085, no_show_rate=0.095,
        patient_cancel_share=0.70,
        wait_mean=31.0, wait_sd=19.0, duration_mean=42.0, duration_sd=16.0,
        walk_in_share=0.06, lead_time_mean=18.0, lead_time_sd=11.0,
        slots_per_weekday=30, occupancy_mean=0.82,
    ),
    DepartmentProfile(
        "DEPT-03", "Orthopaedics", "Musculoskeletal", "FAC-01",
        daily_volume=24.0, cancel_rate=0.095, no_show_rate=0.110,
        patient_cancel_share=0.72,
        wait_mean=36.0, wait_sd=23.0, duration_mean=38.0, duration_sd=15.0,
        walk_in_share=0.05, lead_time_mean=24.0, lead_time_sd=14.0,
        slots_per_weekday=28, occupancy_mean=0.79,
    ),
    DepartmentProfile(
        "DEPT-04", "Paediatrics", "Women & Child", "FAC-01",
        daily_volume=30.0, cancel_rate=0.075, no_show_rate=0.085,
        patient_cancel_share=0.78,
        wait_mean=26.0, wait_sd=16.0, duration_mean=30.0, duration_sd=12.0,
        walk_in_share=0.18, lead_time_mean=9.0, lead_time_sd=7.0,
        slots_per_weekday=34, occupancy_mean=0.84,
    ),
    DepartmentProfile(
        "DEPT-05", "General Medicine", "General & Internal", "FAC-02",
        daily_volume=44.0, cancel_rate=0.065, no_show_rate=0.100,
        patient_cancel_share=0.68,
        wait_mean=29.0, wait_sd=18.0, duration_mean=24.0, duration_sd=10.0,
        walk_in_share=0.14, lead_time_mean=7.0, lead_time_sd=6.0,
        slots_per_weekday=48, occupancy_mean=0.86,
    ),
    DepartmentProfile(
        "DEPT-06", "Diagnostics & Imaging", "Diagnostics", "FAC-02",
        daily_volume=34.0, cancel_rate=0.055, no_show_rate=0.070,
        patient_cancel_share=0.62,
        wait_mean=18.0, wait_sd=11.0, duration_mean=28.0, duration_sd=11.0,
        walk_in_share=0.09, lead_time_mean=5.0, lead_time_sd=4.0,
        slots_per_weekday=40, occupancy_mean=0.90,
    ),
    DepartmentProfile(
        "DEPT-07", "Obstetrics & Gynaecology", "Women & Child", "FAC-02",
        daily_volume=22.0, cancel_rate=0.090, no_show_rate=0.080,
        patient_cancel_share=0.74,
        wait_mean=33.0, wait_sd=21.0, duration_mean=36.0, duration_sd=14.0,
        walk_in_share=0.07, lead_time_mean=15.0, lead_time_sd=10.0,
        slots_per_weekday=26, occupancy_mean=0.81,
    ),
    DepartmentProfile(
        "DEPT-08", "Physiotherapy", "Rehabilitation", "FAC-02",
        daily_volume=28.0, cancel_rate=0.120, no_show_rate=0.140,
        patient_cancel_share=0.86,
        wait_mean=14.0, wait_sd=9.0, duration_mean=44.0, duration_sd=13.0,
        walk_in_share=0.04, lead_time_mean=11.0, lead_time_sd=8.0,
        slots_per_weekday=32, occupancy_mean=0.72,
    ),
)

VISIT_TYPES: tuple[str, ...] = ("new", "follow_up", "procedure", "urgent")
VISIT_TYPE_WEIGHTS: tuple[float, ...] = (0.30, 0.45, 0.12, 0.13)

PATIENT_CANCEL_REASONS: tuple[str, ...] = (
    "patient_unavailable",
    "patient_rescheduled",
    "transport",
)
PROVIDER_CANCEL_REASONS: tuple[str, ...] = (
    "provider_unavailable",
    "schedule_change",
)
FACILITY_CANCEL_REASONS: tuple[str, ...] = (
    "facility_closure",
    "equipment_unavailable",
)

# Terminal appointment statuses, in the exact vocabulary the pipeline expects.
TERMINAL_STATUSES: tuple[str, ...] = ("completed", "cancelled", "no_show")

# ---------------------------------------------------------------------------
# Intraday arrival shaping
# ---------------------------------------------------------------------------

# Relative arrival intensity across clinic hours, used to place scheduled starts.
# Index 0 == 08:00. A morning-weighted profile with a post-lunch secondary peak.
HOUR_WEIGHTS: tuple[float, ...] = (
    1.05, 1.30, 1.55, 1.60, 1.45, 1.20,  # 08:00-13:59
    0.70, 0.95, 1.35, 1.50, 1.30, 0.75,  # 14:00-19:59
)
HOUR_START: int = 8

# Weekday volume multipliers, Monday..Sunday. Weekends run a reduced service.
WEEKDAY_FACTOR: tuple[float, ...] = (1.12, 1.10, 1.06, 1.02, 0.94, 0.42, 0.34)


def _hour_and_minute(rng: np.random.Generator, n: int) -> tuple[np.ndarray, np.ndarray]:
    """Draw ``n`` clinic arrival times shaped by :data:`HOUR_WEIGHTS`."""
    weights = np.asarray(HOUR_WEIGHTS, dtype=float)
    weights = weights / weights.sum()
    hours = rng.choice(len(weights), size=n, p=weights) + HOUR_START
    minutes = rng.integers(0, 60, size=n)
    return hours.astype(int), minutes.astype(int)


def _seasonal_factor(dates: np.ndarray) -> np.ndarray:
    """Mild, smooth annual seasonality (winter respiratory pressure etc.).

    Amplitude is deliberately small (+/- 8%) — enough to make trend charts
    interesting without inventing a dramatic storyline.
    """
    day_of_year = pd.DatetimeIndex(dates).dayofyear.to_numpy()
    return 1.0 + 0.08 * np.sin(2.0 * np.pi * (day_of_year - 15) / 365.25)


def build_departments() -> pd.DataFrame:
    """Return the department dimension."""
    return pd.DataFrame(
        {
            "department_id": [d.department_id for d in DEPARTMENTS],
            "department_name": [d.department_name for d in DEPARTMENTS],
            "specialty_group": [d.specialty_group for d in DEPARTMENTS],
            "facility_id": [d.facility_id for d in DEPARTMENTS],
        }
    )


def build_appointments(
    rng: np.random.Generator,
    anchor: date,
    days: int,
    tz: ZoneInfo,
) -> pd.DataFrame:
    """Generate the synthetic appointment fact table.

    Appointments are booked up to ``lead_time`` days ahead of their scheduled
    slot. Any slot that lands after the anchor date keeps the open status
    ``scheduled`` — these are excluded from rate denominators downstream, which
    is what makes the "exclude open appointments" rule testable.
    """
    calendar = pd.date_range(end=anchor, periods=days, freq="D")
    weekday_factors = np.array([WEEKDAY_FACTOR[d.weekday()] for d in calendar])
    seasonal = _seasonal_factor(calendar.to_numpy())

    frames: list[pd.DataFrame] = []
    appointment_counter = 0

    for profile in DEPARTMENTS:
        # Expected volume per calendar day for this department.
        expected = profile.daily_volume * weekday_factors * seasonal
        # Poisson draw keeps day-to-day variation natural rather than synthetic-smooth.
        daily_counts = rng.poisson(np.maximum(expected, 0.05))
        total = int(daily_counts.sum())
        if total == 0:  # pragma: no cover - defensive
            continue

        # Expand the per-day counts into one scheduled date per appointment.
        scheduled_dates = np.repeat(calendar.to_numpy(), daily_counts)

        hours, minutes = _hour_and_minute(rng, total)

        # --- Booking timestamp: scheduled slot minus a gamma-distributed lead time.
        lead_shape = max((profile.lead_time_mean / max(profile.lead_time_sd, 0.1)) ** 2, 1.2)
        lead_scale = max(profile.lead_time_sd**2 / max(profile.lead_time_mean, 0.1), 0.05)
        lead_days = rng.gamma(shape=lead_shape, scale=lead_scale, size=total)
        lead_days = np.clip(lead_days, 0.02, 120.0)

        # --- Status assignment -------------------------------------------------
        # Draw the terminal outcome, then hold it back for slots after the anchor.
        draw = rng.random(total)
        is_cancelled = draw < profile.cancel_rate
        is_no_show = (~is_cancelled) & (draw < profile.cancel_rate + profile.no_show_rate)
        is_completed = ~(is_cancelled | is_no_show)

        scheduled_local = pd.to_datetime(
            pd.Series(scheduled_dates).dt.strftime("%Y-%m-%d")
            + " "
            + pd.Series(hours).astype(str).str.zfill(2)
            + ":"
            + pd.Series(minutes).astype(str).str.zfill(2)
        )
        created_local = scheduled_local - pd.to_timedelta(lead_days, unit="D")

        scheduled_utc = (
            scheduled_local.dt.tz_localize(tz, nonexistent="shift_forward", ambiguous="NaT")
            .dt.tz_convert(timezone.utc)
            .dt.tz_localize(None)
        )
        created_utc = (
            created_local.dt.tz_localize(tz, nonexistent="shift_forward", ambiguous="NaT")
            .dt.tz_convert(timezone.utc)
            .dt.tz_localize(None)
        )

        # Anything scheduled after the anchor is still open.
        anchor_end_utc = (
            pd.Timestamp(datetime.combine(anchor, datetime.min.time()), tzinfo=tz)
            .tz_convert(timezone.utc)
            .tz_localize(None)
        )
        is_future = scheduled_utc > anchor_end_utc

        status = np.where(
            is_future,
            "scheduled",
            np.where(is_cancelled, "cancelled", np.where(is_no_show, "no_show", "completed")),
        )

        # --- Cancellation reason category (only for cancelled rows) -----------
        reason_draw = rng.random(total)
        reasons = np.where(
            reason_draw < profile.patient_cancel_share,
            rng.choice(PATIENT_CANCEL_REASONS, size=total),
            np.where(
                reason_draw < profile.patient_cancel_share + 0.35,
                rng.choice(PROVIDER_CANCEL_REASONS, size=total),
                rng.choice(FACILITY_CANCEL_REASONS, size=total),
            ),
        )
        cancellation_reason = np.where(status == "cancelled", reasons, None)

        # --- Visit type --------------------------------------------------------
        visit_type = rng.choice(VISIT_TYPES, size=total, p=VISIT_TYPE_WEIGHTS)

        appointment_ids = [
            f"APT-{i:07d}" for i in range(appointment_counter + 1, appointment_counter + total + 1)
        ]
        appointment_counter += total

        frames.append(
            pd.DataFrame(
                {
                    "appointment_id": appointment_ids,
                    "facility_id": profile.facility_id,
                    "department_id": profile.department_id,
                    "appointment_created_at": created_utc.dt.strftime("%Y-%m-%dT%H:%M:%S"),
                    "scheduled_start_at": scheduled_utc.dt.strftime("%Y-%m-%dT%H:%M:%S"),
                    "status": status,
                    "visit_type": visit_type,
                    "cancellation_reason_category": cancellation_reason,
                }
            )
        )

    appointments = pd.concat(frames, ignore_index=True)
    appointments = appointments.sort_values("scheduled_start_at", kind="stable").reset_index(drop=True)
    return appointments


def build_encounters(
    rng: np.random.Generator,
    appointments: pd.DataFrame,
    tz: ZoneInfo,
) -> pd.DataFrame:
    """Generate encounters for attended appointments plus unbooked walk-ins.

    An encounter exists for every appointment that was actually attended
    (status ``completed``). Walk-in encounters carry a NULL ``appointment_id``
    and are given their own department and facility so that encounter-side
    filtering works identically whether or not an appointment exists.
    """
    profile_by_dept = {d.department_id: d for d in DEPARTMENTS}

    # --- Attended appointments ------------------------------------------------
    attended = appointments.loc[appointments["status"] == "completed"].copy()
    attended = attended.reset_index(drop=True)

    n_attended = len(attended)
    dept_ids = attended["department_id"].to_numpy()
    wait_mean = np.array([profile_by_dept[d].wait_mean for d in dept_ids])
    wait_sd = np.array([profile_by_dept[d].wait_sd for d in dept_ids])
    dur_mean = np.array([profile_by_dept[d].duration_mean for d in dept_ids])
    dur_sd = np.array([profile_by_dept[d].duration_sd for d in dept_ids])

    # Wait is gamma-distributed: strictly positive, right-skewed like a real queue.
    wait_shape = np.maximum((wait_mean / np.maximum(wait_sd, 1.0)) ** 2, 1.3)
    wait_scale = np.maximum(wait_sd**2 / np.maximum(wait_mean, 1.0), 0.8)
    waits = rng.gamma(shape=wait_shape, scale=wait_scale)

    # Most appointments arrive a little early; some arrive late.
    arrival_offset = rng.normal(loc=-6.0, scale=7.0, size=n_attended)

    durations = np.clip(
        rng.normal(loc=dur_mean, scale=dur_sd, size=n_attended), 4.0, None
    )

    scheduled = pd.to_datetime(attended["scheduled_start_at"])
    check_in = scheduled + pd.to_timedelta(arrival_offset, unit="m")
    service_start = check_in + pd.to_timedelta(waits, unit="m")
    service_end = service_start + pd.to_timedelta(durations, unit="m")

    # --- Encounter status for attended rows -----------------------------------
    # A small share leave without being seen; a small share are still open at
    # the anchor (recent visits only).
    status_draw = rng.random(n_attended)
    encounter_status = np.where(
        status_draw < 0.035,
        "left_without_being_seen",
        np.where(status_draw < 0.055, "in_progress", "completed"),
    )

    attended_encounters = pd.DataFrame(
        {
            "appointment_id": attended["appointment_id"],
            "facility_id": attended["facility_id"],
            "department_id": attended["department_id"],
            "check_in_at": check_in.dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "service_start_at": service_start.dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "service_end_at": service_end.dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "encounter_status": encounter_status,
            "is_walk_in": False,
        }
    )

    # --- Walk-in encounters (no appointment) ----------------------------------
    anchor = pd.to_datetime(appointments["scheduled_start_at"]).max().date()
    start = anchor - timedelta(days=int((pd.to_datetime(appointments["scheduled_start_at"]).max()
                                         - pd.to_datetime(appointments["scheduled_start_at"]).min()).days))
    calendar = pd.date_range(start=start, end=anchor, freq="D")

    walk_frames: list[pd.DataFrame] = []
    for profile in DEPARTMENTS:
        if profile.walk_in_share <= 0.001:
            continue
        weekday_factors = np.array([WEEKDAY_FACTOR[d.weekday()] for d in calendar])
        expected = profile.daily_volume * profile.walk_in_share * weekday_factors
        daily_counts = rng.poisson(np.maximum(expected, 0.02))
        total = int(daily_counts.sum())
        if total == 0:  # pragma: no cover - defensive
            continue

        walk_dates = np.repeat(calendar.to_numpy(), daily_counts)
        hours, minutes = _hour_and_minute(rng, total)

        check_in_local = pd.to_datetime(
            pd.Series(walk_dates).dt.strftime("%Y-%m-%d")
            + " "
            + pd.Series(hours).astype(str).str.zfill(2)
            + ":"
            + pd.Series(minutes).astype(str).str.zfill(2)
        )

        # Walk-in waits run longer: no booked slot to anchor against.
        waits_w = rng.gamma(
            shape=max((profile.wait_mean * 1.35 / max(profile.wait_sd, 1.0)) ** 2, 1.3),
            scale=max(profile.wait_sd**2 / max(profile.wait_mean * 1.35, 1.0), 0.8),
            size=total,
        )
        durations_w = np.clip(
            rng.normal(loc=profile.duration_mean, scale=profile.duration_sd, size=total),
            4.0, None,
        )

        service_start_w = check_in_local + pd.to_timedelta(waits_w, unit="m")
        service_end_w = service_start_w + pd.to_timedelta(durations_w, unit="m")

        status_draw_w = rng.random(total)
        status_w = np.where(
            status_draw_w < 0.075,
            "left_without_being_seen",
            np.where(status_draw_w < 0.10, "in_progress", "completed"),
        )

        walk_frames.append(
            pd.DataFrame(
                {
                    "appointment_id": None,
                    "facility_id": profile.facility_id,
                    "department_id": profile.department_id,
                    "check_in_at": check_in_local.dt.strftime("%Y-%m-%dT%H:%M:%S"),
                    "service_start_at": service_start_w.dt.strftime("%Y-%m-%dT%H:%M:%S"),
                    "service_end_at": service_end_w.dt.strftime("%Y-%m-%dT%H:%M:%S"),
                    "encounter_status": status_w,
                    "is_walk_in": True,
                }
            )
        )

    walk_encounters = pd.concat(walk_frames, ignore_index=True) if walk_frames else pd.DataFrame()

    encounters = pd.concat([attended_encounters, walk_encounters], ignore_index=True)
    encounters = encounters.sort_values("check_in_at", kind="stable").reset_index(drop=True)
    encounters.insert(
        0, "encounter_id", [f"ENC-{i:07d}" for i in range(1, len(encounters) + 1)]
    )
    encounters = encounters.drop(columns=["is_walk_in"])
    return encounters


def build_capacity_daily(
    rng: np.random.Generator,
    anchor: date,
    days: int,
) -> pd.DataFrame:
    """Generate optional daily capacity/occupancy inputs.

    Capacity is deliberately a *separate, optional* input: if it is absent the
    platform must report utilisation as unavailable rather than inventing it.
    Occupancy is capped at available slots so utilisation can never exceed 100%.
    """
    calendar = pd.date_range(end=anchor, periods=days, freq="D")
    rows: list[pd.DataFrame] = []

    for profile in DEPARTMENTS:
        weekday_factors = np.array([WEEKDAY_FACTOR[d.weekday()] for d in calendar])
        # Available slots scale with the weekday, with a little slack around it.
        available = np.maximum(
            np.round(profile.slots_per_weekday * weekday_factors * rng.normal(1.0, 0.05, len(calendar))),
            1,
        ).astype(int)

        # Occupancy tracks the seasonal pattern so busy weeks genuinely look busier.
        seasonal = _seasonal_factor(calendar.to_numpy())
        occupancy_rate = np.clip(
            profile.occupancy_mean * seasonal + rng.normal(0.0, 0.055, len(calendar)),
            0.35, 0.99,
        )
        occupied = np.minimum(np.round(available * occupancy_rate), available).astype(int)
        staffed = np.minimum(
            np.round(available * rng.uniform(0.88, 1.0, len(calendar))), available
        ).astype(int)

        rows.append(
            pd.DataFrame(
                {
                    "date": calendar.strftime("%Y-%m-%d"),
                    "facility_id": profile.facility_id,
                    "department_id": profile.department_id,
                    "available_slots": available,
                    "staffed_slots": staffed,
                    "occupied_slots": occupied,
                }
            )
        )

    return pd.concat(rows, ignore_index=True).sort_values(
        ["date", "department_id"], kind="stable"
    ).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Intentionally flawed fixture
# ---------------------------------------------------------------------------

def build_flawed_appointments(appointments: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """Return a small appointment extract seeded with realistic data defects.

    This fixture exists so the data-quality view can demonstrate FAILING checks
    alongside passing ones. It is written to ``data/raw/flawed/`` and is never
    loaded into the analytical store. Defect classes injected, one per category
    the validation layer claims to catch:

        * missing required value   (null department_id, null scheduled_start_at)
        * malformed timestamp      (non-ISO text in scheduled_start_at)
        * invalid status           (status outside the controlled vocabulary)
        * duplicate primary key    (repeated appointment_id)
        * referential integrity    (department_id with no matching department)
    """
    sample = appointments.sample(n=40, random_state=1).copy().reset_index(drop=True)

    # 1. Missing required value.
    sample.loc[0, "department_id"] = None
    sample.loc[1, "scheduled_start_at"] = None

    # 2. Malformed timestamp.
    sample.loc[2, "scheduled_start_at"] = "2026-13-45 99:99:99"
    sample.loc[3, "appointment_created_at"] = "not-a-timestamp"

    # 3. Unknown status value.
    sample.loc[4, "status"] = "rescheduled_elsewhere"
    sample.loc[5, "status"] = ""

    # 4. Duplicate primary key (repeat an existing appointment_id).
    sample.loc[6, "appointment_id"] = sample.loc[7, "appointment_id"]

    # 5. Orphan department reference.
    sample.loc[8, "department_id"] = "DEPT-99"
    sample.loc[9, "facility_id"] = "FAC-99"

    # 6. A cancelled appointment with no reason category, and a completed one
    #    that carries a cancellation reason — both internally inconsistent.
    sample.loc[10, "status"] = "cancelled"
    sample.loc[10, "cancellation_reason_category"] = None
    sample.loc[11, "status"] = "completed"
    sample.loc[11, "cancellation_reason_category"] = "patient_unavailable"

    return sample


def build_flawed_encounters(encounters: pd.DataFrame) -> pd.DataFrame:
    """Return an encounter extract seeded with impossible-duration defects."""
    sample = encounters.sample(n=25, random_state=2).copy().reset_index(drop=True)

    # Negative wait: service starts before the patient checked in.
    sample.loc[0, "service_start_at"] = "2026-01-01T08:00:00"
    sample.loc[0, "check_in_at"] = "2026-01-01T09:30:00"

    # Implausible wait: a queue time beyond 24 hours.
    sample.loc[1, "check_in_at"] = "2026-01-01T08:00:00"
    sample.loc[1, "service_start_at"] = "2026-01-03T09:00:00"

    # Negative visit duration: service ends before it starts.
    sample.loc[2, "service_start_at"] = "2026-01-01T10:00:00"
    sample.loc[2, "service_end_at"] = "2026-01-01T09:00:00"

    # Missing check-in.
    sample.loc[3, "check_in_at"] = None

    # Orphan appointment reference.
    sample.loc[4, "appointment_id"] = "APT-9999999"

    return sample


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--seed", type=int, default=settings.synthetic_seed,
                        help="RNG seed; identical seed => identical CSVs")
    parser.add_argument("--days", type=int, default=settings.synthetic_days,
                        help="days of history to generate, ending on the anchor")
    parser.add_argument("--anchor", type=str, default=settings.synthetic_anchor_date,
                        help="last date of the generated history (YYYY-MM-DD)")
    parser.add_argument("--out", type=Path, default=settings.raw_dir,
                        help="output directory for the CSVs")
    args = parser.parse_args(argv)

    anchor = datetime.strptime(args.anchor, "%Y-%m-%d").date()
    tz = settings.tzinfo
    rng = np.random.default_rng(args.seed)

    out_dir: Path = args.out
    flawed_dir = out_dir / "flawed"
    out_dir.mkdir(parents=True, exist_ok=True)
    flawed_dir.mkdir(parents=True, exist_ok=True)

    print(f"Generating synthetic dataset  seed={args.seed} days={args.days} anchor={anchor}")

    departments = build_departments()
    appointments = build_appointments(rng, anchor, args.days, tz)
    encounters = build_encounters(rng, appointments, tz)
    capacity = build_capacity_daily(rng, anchor, args.days)

    departments.to_csv(out_dir / "departments.csv", index=False)
    appointments.to_csv(out_dir / "appointments.csv", index=False)
    encounters.to_csv(out_dir / "encounters.csv", index=False)
    capacity.to_csv(out_dir / "capacity_daily.csv", index=False)

    flawed_appointments = build_flawed_appointments(appointments, rng)
    flawed_encounters = build_flawed_encounters(encounters)
    flawed_appointments.to_csv(flawed_dir / "appointments.csv", index=False)
    flawed_encounters.to_csv(flawed_dir / "encounters.csv", index=False)

    print(f"  departments.csv          {len(departments):>8,} rows")
    print(f"  appointments.csv         {len(appointments):>8,} rows")
    print(f"  encounters.csv           {len(encounters):>8,} rows")
    print(f"  capacity_daily.csv       {len(capacity):>8,} rows")
    print(f"  flawed/appointments.csv  {len(flawed_appointments):>8,} rows (intentionally invalid)")
    print(f"  flawed/encounters.csv    {len(flawed_encounters):>8,} rows (intentionally invalid)")
    print(f"Written to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

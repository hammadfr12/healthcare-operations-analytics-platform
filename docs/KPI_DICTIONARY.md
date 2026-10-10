# KPI Dictionary

The authoritative definition of every metric the platform publishes.

Every figure here is computed in exactly one place — `backend/app/metrics.py` —
and read by the dashboard API, the CSV exports and the Tableau extract alike.
That is a structural guarantee, not a convention: there is no second
implementation to drift.

## Three rules that apply to every metric

1. **A zero denominator yields `null`, never `0`.** The API returns
   `"value": null, "available": false` with a reason. "0%" and "undefined" are
   different claims and are never conflated. The UI renders `null` as `N/A`.
2. **Rate metrics are recomputed from summed numerators and denominators**, not
   averaged from daily rates. Averaging daily rates would weight a quiet Sunday
   equally with a busy Monday.
3. **Sample size travels with every aggregate.** A mean wait with n=3 is not
   the same claim as a mean wait with n=3,000, and the response says which it is
   in `sample_size`.

## The terminal-appointment denominator

Four metrics below divide by **terminal appointments**: those in a state of
`completed`, `cancelled` or `no_show`. Appointments still `scheduled` or in the
future are excluded from both numerator and denominator.

This matters. A cancellation rate computed over *all* appointments falls
mechanically as more future bookings are added, without a single cancellation
behaving differently. Anchoring to terminal appointments makes the metric
comparable across windows of different lengths and across time.

The caveat text is attached to the metric itself and travels with it into every
API response and CSV export.

---

## Headline KPIs

### `appointments_scheduled` — Appointments scheduled

| | |
|---|---|
| Unit | count |
| Numerator | Distinct appointments with `scheduled_start_at` in the window |
| Source | `core.fct_appointments` |

Counted on the **scheduled** date basis, not the booking date. An appointment
booked in June for a date in July counts in July.

### `completed_visits` — Completed visits

| | |
|---|---|
| Unit | count |
| Numerator | Encounters with a completed encounter status |
| Source | `core.fct_encounters` |

**Not comparable with `appointments_scheduled`.** This counts *encounters*, and
a walk-in patient has an encounter but no appointment. The two figures answer
different questions and the metric carries that caveat explicitly.

### `cancellation_rate` — Cancellation rate

| | |
|---|---|
| Unit | percent |
| Numerator | Cancelled appointments in the window |
| Denominator | Terminal appointments in the window |

Patient-, provider- and facility-initiated cancellations are **combined** in
this headline figure. The split by reason and initiator is published separately
on the Appointments page, so the combination is always inspectable rather than
buried.

### `no_show_rate` — No-show rate

| | |
|---|---|
| Unit | percent |
| Numerator | No-show appointments in the window |
| Denominator | Terminal appointments in the window |

### `lost_slot_rate` — Lost slot rate

| | |
|---|---|
| Unit | percent |
| Numerator | Cancelled + no-show appointments |
| Denominator | Terminal appointments in the window |

A convenience roll-up of the two rates above. Because the denominator is
identical, this can never exceed `cancellation_rate + no_show_rate` — a useful
arithmetic invariant when reconciling an export.

### `median_wait_minutes` — Median wait

| | |
|---|---|
| Unit | minutes |
| Value | Median of `service_start_at − check_in_at` over valid waits |
| Source | `core.fct_encounters` |

Computed per bucket directly from the fact table. A median cannot be recovered
from daily sums, which is why this metric does not read the daily mart.

**Exclusions applied before the median is taken:** negative waits (service
started before check-in) and implausibly long waits (>24 hours). Both counts are
reported as `wait_negative_count` and `wait_implausible_count` in the patient
flow summary, so the exclusion is visible rather than silent.

### `average_wait_minutes` — Average wait

| | |
|---|---|
| Unit | minutes |
| Value | Mean of valid wait durations |

Can be skewed by outliers. Always read alongside the median and
`sample_size`. A mean far above the median indicates a long right tail, not a
uniformly slower department.

### `average_visit_duration_minutes` — Average visit duration

| | |
|---|---|
| Unit | minutes |
| Value | Mean of `service_end_at − service_start_at` over valid durations |

Visits exceeding 4 hours are **included** in the mean but flagged via
`is_duration_outlier` on the fact table and counted in
`duration_outlier_count`. They are not silently dropped.

### `appointment_lead_time_days` — Appointment lead time

| | |
|---|---|
| Unit | days |
| Value | Mean of `scheduled_start_at − appointment_created_at` |
| Source | `core.fct_appointments` |

Requires a booking-created timestamp. Appointments without one are excluded
from both numerator and denominator, and the excluded count is available as
`lead_time_valid_count` on the daily mart.

### `capacity_utilisation` — Capacity utilisation

| | |
|---|---|
| Unit | percent |
| Numerator | Occupied slots over days with usable capacity data |
| Denominator | Available slots over those same days |
| Source | `core.capacity_daily` |

**Published only when reliable capacity and occupancy inputs are supplied.**
Days with missing capacity data are excluded from *both* sides of the ratio —
including them on one side only would bias the figure. When no usable days
exist in the window, the metric returns `null` with
`capacity_available: false` rather than reporting 0%.

---

## Ranking guard

Departments are compared and ranked on a page that reports `rankable` per row.

A department with fewer than **30 terminal appointments** in the window is
returned with `rankable: false` and a reason, rather than being hidden or ranked
on noise. A 0% or 100% no-show rate computed from four appointments is not a
finding; it is an artifact of the sample size, and ranking on it would mislead.

The row is still returned with its real numbers — the flag qualifies the
comparison, it does not censor the data.

The same threshold (`MIN_TERMINAL_SAMPLE = 30`) and the wait-sample threshold
(`MIN_WAIT_SAMPLE = 20`) are defined once, in `backend/app/metrics.py`.

---

## Period-over-period change

Every KPI carries a comparison against the **immediately preceding window of
equal length**. A 90-day window compares against the 90 days before it.

`change_percent` is `null` with `change_available: false` and an explicit reason
whenever a percentage change would be meaningless — most importantly when the
comparison value is zero, where the change is undefined rather than infinite.

---

## Reconciliation

To verify a number end to end:

```bash
make pipeline
```

Then compare a KPI against a direct query. The seed is fixed
(`HOA_SYNTHETIC_SEED=20261010`), so the same store is rebuilt every time and
these figures are reproducible:

```sql
-- Cancellation rate for a window, computed independently of the API.
SELECT
    SUM(cancelled_count) * 100.0 / SUM(terminal_count) AS cancellation_rate,
    SUM(terminal_count)                                AS denominator,
    SUM(cancelled_count)                               AS numerator
FROM analytics.mart_appointments_daily
WHERE metric_date BETWEEN DATE '2026-07-13' AND DATE '2026-10-10';
```

Compare against:

```bash
curl 'http://127.0.0.1:8000/api/metrics/kpis?start_date=2026-07-13&end_date=2026-10-10'
```

`numerator` and `denominator` are returned on every rate metric precisely so
this check is possible without re-deriving the definition.

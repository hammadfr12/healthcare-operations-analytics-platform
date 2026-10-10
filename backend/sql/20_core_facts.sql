-- ===========================================================================
-- 20_core_facts.sql
-- Conformed facts with every KPI input derived once, in one place.
--
-- All KPI arithmetic that depends on a definition choice (which statuses are
-- terminal, which durations are impossible) is resolved HERE, so the API, the
-- CSV exports and the Tableau extracts cannot drift apart.
--
-- Timestamps are stored in UTC. Display timezone is applied at the presentation
-- layer only, so no metric changes when the facility timezone is reconfigured.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- fct_appointments — one row per accepted appointment.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE core.fct_appointments AS
SELECT
    a.appointment_id,
    a.facility_id,
    a.department_id,
    a.visit_type,
    a.status                                   AS appointment_status,
    a.cancellation_reason_category,

    CAST(a.appointment_created_at AS TIMESTAMP) AS appointment_created_at,
    CAST(a.scheduled_start_at    AS TIMESTAMP)  AS scheduled_start_at,
    CAST(a.scheduled_start_at AS DATE)          AS appointment_date,

    -- Terminal outcomes are the denominator for cancellation / no-show rates.
    -- Open ("scheduled") appointments are excluded so an upcoming clinic cannot
    -- dilute the rate. See docs/KPI_DICTIONARY.md.
    (a.status IN ('completed', 'cancelled', 'no_show')) AS is_terminal,
    (a.status = 'completed')  AS is_completed,
    (a.status = 'cancelled')  AS is_cancelled,
    (a.status = 'no_show')    AS is_no_show,
    (a.status = 'scheduled')  AS is_open,

    -- Appointment lead time (booking -> scheduled start), in days.
    -- Negative values mean the appointment was scheduled before it was booked,
    -- which is impossible; those are nulled out and counted as invalid.
    CASE
        WHEN a.appointment_created_at IS NULL OR a.scheduled_start_at IS NULL THEN NULL
        WHEN CAST(a.scheduled_start_at AS TIMESTAMP)
             < CAST(a.appointment_created_at AS TIMESTAMP) THEN NULL
        ELSE date_diff('second',
                       CAST(a.appointment_created_at AS TIMESTAMP),
                       CAST(a.scheduled_start_at    AS TIMESTAMP)) / 86400.0
    END AS lead_time_days,

    CASE
        WHEN a.appointment_created_at IS NOT NULL
         AND a.scheduled_start_at IS NOT NULL
         AND CAST(a.scheduled_start_at AS TIMESTAMP)
             >= CAST(a.appointment_created_at AS TIMESTAMP)
        THEN TRUE ELSE FALSE
    END AS lead_time_is_valid

FROM staging.appointments AS a;

-- ---------------------------------------------------------------------------
-- fct_encounters — one row per accepted encounter (the patient-flow fact).
--
-- Walk-in encounters carry no appointment. They are retained because they
-- consume real capacity, and are labelled visit_type = 'unscheduled' with
-- appointment_status = 'unscheduled' so that filtering by either dimension
-- stays consistent between the appointment and encounter sides.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE core.fct_encounters AS
SELECT
    e.encounter_id,
    e.appointment_id,
    e.facility_id,
    e.department_id,
    e.encounter_status,

    COALESCE(a.visit_type, 'unscheduled')          AS visit_type,
    COALESCE(a.appointment_status, 'unscheduled')  AS appointment_status,
    (e.appointment_id IS NULL)                     AS is_walk_in,

    CAST(e.check_in_at     AS TIMESTAMP) AS check_in_at,
    CAST(e.service_start_at AS TIMESTAMP) AS service_start_at,
    CAST(e.service_end_at   AS TIMESTAMP) AS service_end_at,
    CAST(e.check_in_at AS DATE)           AS encounter_date,

    -- Completed visits are the PRD's "completed visits" metric: distinct
    -- encounter IDs in a completed state. Never equated with appointment count.
    (e.encounter_status = 'completed') AS is_completed_visit,

    -- Wait time = service start - check-in, in minutes.
    -- A negative wait is impossible; a wait beyond 24h indicates a data error
    -- rather than a real queue. Both are excluded from aggregates and surfaced
    -- as invalid counts instead of being silently dropped.
    CASE
        WHEN e.check_in_at IS NULL OR e.service_start_at IS NULL THEN NULL
        WHEN CAST(e.service_start_at AS TIMESTAMP) < CAST(e.check_in_at AS TIMESTAMP) THEN NULL
        WHEN date_diff('second', CAST(e.check_in_at AS TIMESTAMP),
                       CAST(e.service_start_at AS TIMESTAMP)) > 86400 THEN NULL
        ELSE date_diff('second', CAST(e.check_in_at AS TIMESTAMP),
                       CAST(e.service_start_at AS TIMESTAMP)) / 60.0
    END AS wait_minutes,

    CASE
        WHEN e.check_in_at IS NULL OR e.service_start_at IS NULL THEN 'missing_timestamp'
        WHEN CAST(e.service_start_at AS TIMESTAMP) < CAST(e.check_in_at AS TIMESTAMP) THEN 'negative_wait'
        WHEN date_diff('second', CAST(e.check_in_at AS TIMESTAMP),
                       CAST(e.service_start_at AS TIMESTAMP)) > 86400 THEN 'implausible_wait'
        ELSE NULL
    END AS wait_invalid_reason,

    -- Visit duration = service end - service start, in minutes. Same rules.
    CASE
        WHEN e.service_start_at IS NULL OR e.service_end_at IS NULL THEN NULL
        WHEN CAST(e.service_end_at AS TIMESTAMP) < CAST(e.service_start_at AS TIMESTAMP) THEN NULL
        ELSE date_diff('second', CAST(e.service_start_at AS TIMESTAMP),
                       CAST(e.service_end_at AS TIMESTAMP)) / 60.0
    END AS visit_duration_minutes,

    -- Duration outliers: beyond 4 hours of face-to-face service time. Flagged,
    -- never dropped, so the UI can show how much the mean is being pulled.
    (e.service_start_at IS NOT NULL AND e.service_end_at IS NOT NULL
     AND CAST(e.service_end_at AS TIMESTAMP) >= CAST(e.service_start_at AS TIMESTAMP)
     AND date_diff('second', CAST(e.service_start_at AS TIMESTAMP),
                   CAST(e.service_end_at AS TIMESTAMP)) > 14400) AS is_duration_outlier

FROM staging.encounters AS e
LEFT JOIN core.fct_appointments AS a
       ON a.appointment_id = e.appointment_id;

-- ---------------------------------------------------------------------------
-- capacity_daily — capacity is optional by design. When the supplied source
-- has no usable capacity fields the table is empty and every utilisation KPI
-- reports "unavailable" rather than inventing a number (PRD §13).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE core.capacity_daily AS
SELECT
    CAST(c.date AS DATE) AS metric_date,
    c.facility_id,
    c.department_id,
    TRY_CAST(c.available_slots AS INTEGER) AS available_slots,
    TRY_CAST(c.staffed_slots   AS INTEGER) AS staffed_slots,
    TRY_CAST(c.occupied_slots  AS INTEGER) AS occupied_slots,

    -- Utilisation is only defined when both inputs are present and the
    -- denominator is positive. Otherwise NULL => rendered as "unavailable".
    CASE
        WHEN TRY_CAST(c.available_slots AS INTEGER) IS NULL
          OR TRY_CAST(c.occupied_slots  AS INTEGER) IS NULL
          OR TRY_CAST(c.available_slots AS INTEGER) <= 0
        THEN NULL
        ELSE LEAST(
                 TRY_CAST(c.occupied_slots AS INTEGER) * 100.0
                 / TRY_CAST(c.available_slots AS INTEGER),
                 100.0)
    END AS utilisation_rate

FROM staging.capacity_daily AS c
WHERE TRY_CAST(c.available_slots AS INTEGER) IS NOT NULL
  AND TRY_CAST(c.available_slots AS INTEGER) > 0;

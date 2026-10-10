-- ===========================================================================
-- 30_analytics_marts.sql
-- Pre-aggregated analytical marts consumed by the API and the Tableau extracts.
--
-- DESIGN NOTE — why daily additive marts and not pre-computed rates:
-- Every measure stored here is a COUNT or a SUM, so it can be re-aggregated
-- correctly over ANY date range, department subset or visit-type filter by
-- simple addition. Rates (cancellation %, no-show %) are therefore deliberately
-- NOT stored: averaging daily rates would silently weight a quiet Sunday the
-- same as a busy Monday. The API divides the summed numerators by the summed
-- denominators instead.
--
-- Median and percentile metrics cannot be derived from daily aggregates, so
-- they are computed on the fly from core.fct_encounters, which is indexed by
-- date. At the bundled demo scale this is a sub-millisecond operation.
--
-- Filterable dimensions are carried as columns (not folded away) so the same
-- mart serves every dashboard filter combination without a separate table.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- mart_appointments_daily
-- Grain: appointment date x facility x department x visit type x status.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE analytics.mart_appointments_daily AS
SELECT
    a.appointment_date                     AS metric_date,
    a.facility_id,
    a.department_id,
    a.visit_type,
    a.appointment_status,

    COUNT(*)                               AS appointment_count,
    COUNT(*) FILTER (WHERE a.is_terminal)  AS terminal_count,
    COUNT(*) FILTER (WHERE a.is_completed) AS completed_count,
    COUNT(*) FILTER (WHERE a.is_cancelled) AS cancelled_count,
    COUNT(*) FILTER (WHERE a.is_no_show)   AS no_show_count,
    COUNT(*) FILTER (WHERE a.is_open)      AS open_count,

    -- Lead-time numerator/denominator kept separate so the mean can be
    -- re-derived over any range without re-scanning the fact table.
    COUNT(*) FILTER (WHERE a.lead_time_is_valid) AS lead_time_valid_count,
    COALESCE(SUM(a.lead_time_days) FILTER (WHERE a.lead_time_is_valid), 0.0)
                                                 AS lead_time_sum_days

FROM core.fct_appointments AS a
GROUP BY ALL;

-- ---------------------------------------------------------------------------
-- mart_encounters_daily
-- Grain: encounter date x facility x department x visit type x status.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE analytics.mart_encounters_daily AS
SELECT
    e.encounter_date                       AS metric_date,
    e.facility_id,
    e.department_id,
    e.visit_type,
    e.appointment_status,
    e.encounter_status,

    COUNT(*)                                        AS encounter_count,
    COUNT(*) FILTER (WHERE e.is_completed_visit)    AS completed_visit_count,
    COUNT(*) FILTER (WHERE e.is_walk_in)            AS walk_in_count,

    -- Wait time: valid sample count travels with the sum so the UI can always
    -- show "n = ..." beside a mean (PRD KPI dictionary caveat).
    COUNT(*) FILTER (WHERE e.wait_minutes IS NOT NULL)  AS wait_valid_count,
    COALESCE(SUM(e.wait_minutes), 0.0)                  AS wait_sum_minutes,
    COUNT(*) FILTER (WHERE e.wait_invalid_reason IS NOT NULL) AS wait_invalid_count,
    COUNT(*) FILTER (WHERE e.wait_invalid_reason = 'negative_wait')    AS wait_negative_count,
    COUNT(*) FILTER (WHERE e.wait_invalid_reason = 'implausible_wait') AS wait_implausible_count,

    -- Visit duration: same numerator/denominator treatment.
    COUNT(*) FILTER (WHERE e.visit_duration_minutes IS NOT NULL) AS duration_valid_count,
    COALESCE(SUM(e.visit_duration_minutes), 0.0)                 AS duration_sum_minutes,
    COUNT(*) FILTER (WHERE e.is_duration_outlier)                AS duration_outlier_count

FROM core.fct_encounters AS e
GROUP BY ALL;

-- ---------------------------------------------------------------------------
-- mart_flow_distribution_daily
-- Grain: encounter date x facility x department x wait bucket.
--
-- Powers the patient-flow wait histogram. Bucketing in SQL keeps the API free
-- of presentation logic and makes the distribution directly usable in Tableau
-- as a binned dimension. Only VALID waits are bucketed; invalid ones are
-- reported separately so the histogram is never quietly skewed.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE analytics.mart_flow_distribution_daily AS
WITH bucketed AS (
    SELECT
        e.encounter_date AS metric_date,
        e.facility_id,
        e.department_id,
        e.visit_type,
        e.wait_minutes,
        -- 15-minute buckets up to 4 hours, then a single overflow bucket.
        CASE
            WHEN e.wait_minutes IS NULL THEN NULL
            WHEN e.wait_minutes >= 240  THEN 240
            ELSE CAST(FLOOR(e.wait_minutes / 15.0) * 15 AS INTEGER)
        END AS wait_bucket_lower_minutes
    FROM core.fct_encounters AS e
    WHERE e.wait_minutes IS NOT NULL
)
SELECT
    metric_date,
    facility_id,
    department_id,
    visit_type,
    wait_bucket_lower_minutes,
    -- Human-readable bucket label, e.g. "30-45 min" / "240+ min".
    CASE
        WHEN wait_bucket_lower_minutes >= 240
            THEN '240+ min'
        ELSE CAST(wait_bucket_lower_minutes AS VARCHAR) || '-'
             || CAST(wait_bucket_lower_minutes + 15 AS VARCHAR) || ' min'
    END AS wait_bucket_label,
    COUNT(*)              AS encounter_count,
    SUM(wait_minutes)     AS wait_sum_minutes
FROM bucketed
WHERE wait_bucket_lower_minutes IS NOT NULL
GROUP BY ALL;

-- ---------------------------------------------------------------------------
-- mart_capacity_daily
-- Grain: date x facility x department. Empty when the source has no usable
-- capacity fields — the API then reports utilisation as "unavailable".
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE analytics.mart_capacity_daily AS
SELECT
    c.metric_date,
    c.facility_id,
    c.department_id,

    COUNT(*)                                            AS day_count,
    SUM(c.available_slots)                              AS available_slots,
    SUM(c.staffed_slots)                                AS staffed_slots,
    SUM(c.occupied_slots)                               AS occupied_slots,

    -- Count of days in the group that carry a definable utilisation rate, so
    -- the API can decide whether a utilisation figure is publishable at all.
    COUNT(*) FILTER (WHERE c.utilisation_rate IS NOT NULL) AS utilisation_valid_days,
    SUM(c.occupied_slots) FILTER (WHERE c.utilisation_rate IS NOT NULL)
                                                        AS occupied_slots_valid,
    SUM(c.available_slots) FILTER (WHERE c.utilisation_rate IS NOT NULL)
                                                        AS available_slots_valid

FROM core.capacity_daily AS c
GROUP BY ALL;

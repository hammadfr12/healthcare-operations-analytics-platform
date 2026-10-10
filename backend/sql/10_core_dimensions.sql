-- ===========================================================================
-- 10_core_dimensions.sql
-- Conformed dimensions built from validated staging rows.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- dim_department — the authoritative department label + specialty grouping.
-- Orphan department references are rejected upstream by the validation layer,
-- so a straight projection is safe here.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE core.dim_department AS
SELECT
    department_id,
    department_name,
    specialty_group
FROM staging.departments;

-- ---------------------------------------------------------------------------
-- dim_facility — facilities are an operational dimension referenced by
-- appointments, encounters and capacity. The demo ships two synthetic sites.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE core.dim_facility AS
SELECT
    facility_id,
    -- Neutral, obviously-synthetic labels: no real-world hospital branding.
    CASE facility_id
        WHEN 'FAC-01' THEN 'Demo Facility North'
        WHEN 'FAC-02' THEN 'Demo Facility South'
        ELSE 'Demo Facility ' || facility_id
    END AS facility_name
FROM (SELECT DISTINCT facility_id FROM staging.appointments) AS f;

-- ---------------------------------------------------------------------------
-- dim_date — contiguous calendar spine spanning the loaded data. Trend charts
-- and period comparisons need gap-free dates so a zero-activity day renders as
-- zero rather than being silently skipped.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE core.dim_date AS
WITH bounds AS (
    -- Aggregate each source separately, then combine. A single FROM over both
    -- tables would be a cartesian product (O(n*m)) for no benefit.
    SELECT
        LEAST(
            (SELECT MIN(CAST(scheduled_start_at AS DATE)) FROM staging.appointments),
            (SELECT MIN(CAST(check_in_at AS DATE)) FROM staging.encounters)
        ) AS min_date,
        GREATEST(
            (SELECT MAX(CAST(scheduled_start_at AS DATE)) FROM staging.appointments),
            (SELECT MAX(CAST(check_in_at AS DATE)) FROM staging.encounters)
        ) AS max_date
)
SELECT
    CAST(d AS DATE)                                        AS date_day,
    date_trunc('week', CAST(d AS DATE))                    AS date_week,
    date_trunc('month', CAST(d AS DATE))                   AS date_month,
    CAST(strftime(CAST(d AS DATE), '%Y') AS INTEGER)       AS year_number,
    CAST(strftime(CAST(d AS DATE), '%m') AS INTEGER)       AS month_number,
    CAST(strftime(CAST(d AS DATE), '%d') AS INTEGER)       AS day_of_month,
    CAST(strftime(CAST(d AS DATE), '%w') AS INTEGER)       AS day_of_week,
    -- Weekend flag is derived for context only; it is never used as a KPI.
    (CAST(strftime(CAST(d AS DATE), '%w') AS INTEGER) IN (0, 6)) AS is_weekend
FROM bounds, generate_series(bounds.min_date, bounds.max_date, INTERVAL 1 DAY) AS t(d);

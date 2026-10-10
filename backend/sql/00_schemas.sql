-- ===========================================================================
-- 00_schemas.sql
-- Layer scaffolding for the local DuckDB analytical store.
--
-- Layering contract (PRD §8):
--   raw        -> untouched CSV landing zone on disk (never modified)
--   staging    -> typed, validated rows accepted by the pipeline (DuckDB)
--   core       -> conformed, de-duplicated facts and dimensions (DuckDB)
--   analytics  -> pre-aggregated marts for the API and Tableau extracts
--   quality    -> pipeline observability (runs + issues)
--
-- Rebuilds are idempotent: every downstream layer is dropped and recreated so
-- a rerun can never leave a half-updated store behind.
-- ===========================================================================

CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS analytics;
CREATE SCHEMA IF NOT EXISTS quality;

-- The store is single-writer and rebuilt wholesale; drop downstream layers so
-- a schema change cannot silently leave stale columns in place.
DROP TABLE IF EXISTS analytics.mart_flow_distribution_daily;
DROP TABLE IF EXISTS analytics.mart_capacity_daily;
DROP TABLE IF EXISTS analytics.mart_encounters_daily;
DROP TABLE IF EXISTS analytics.mart_appointments_daily;

DROP TABLE IF EXISTS core.fct_encounters;
DROP TABLE IF EXISTS core.fct_appointments;
DROP TABLE IF EXISTS core.capacity_daily;
DROP TABLE IF EXISTS core.dim_department;
DROP TABLE IF EXISTS core.dim_facility;
DROP TABLE IF EXISTS core.dim_date;

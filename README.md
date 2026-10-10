# Healthcare Operations Analytics Platform

Operational analytics over appointment, encounter and capacity data for a
multi-facility healthcare provider.

The platform answers questions like: how long are patients waiting, which
departments are losing slots to cancellations, and is capacity being used
efficiently — with every number defensible and every definition published.

---

## The design decision that shapes everything else

**Every metric is computed once, in `backend/app/metrics.py`.**

The dashboard API, the CSV exports and the Tableau extract all call the same
functions. This is not a convention that has to be maintained — there is no
second implementation to drift out of sync. When a definition changes, it
changes in one place, and a downloaded CSV can never disagree with the screen it
came from.

Three rules are enforced throughout, and they are the reason the numbers are
trustworthy rather than merely plausible:

1. **A zero denominator yields `null`, never `0`.** A cancellation rate of "0%"
   and a cancellation rate of "undefined" are different claims. The API returns
   `"available": false` with a reason, and the UI renders `N/A`.
2. **Rate metrics are recomputed from summed numerators and denominators**, not
   averaged from daily rates. Averaging daily rates weights a quiet Sunday
   equally with a busy Monday.
3. **Sample size travels with every aggregate.** A mean wait with n=3 is not the
   same claim as a mean wait with n=3,000, and the response says which it is.

A fourth rule applies to comparison: departments with fewer than 30 terminal
appointments are flagged `rankable: false` rather than ranked. A 100% no-show
rate computed from four appointments is an artifact, not a finding.

---

## Quick start

```bash
make install     # create .venv and install dependencies
make data        # generate the seeded synthetic dataset
make pipeline    # build the DuckDB analytical store
make api         # serve on http://127.0.0.1:8000
```

Then open <http://127.0.0.1:8000/docs> for the interactive API reference.

No external credentials, services or API keys are required. Every setting has a
working default; `.env` is optional for the demo.

### Verify it works

```bash
make test
```

---

## Architecture

```
data/raw/*.csv
      │
      ▼
┌─────────────────┐
│  validation.py  │  every row checked; errors quarantined, warnings reported
└────────┬────────┘
         ▼
┌─────────────────┐
│   pipeline.py   │  writes to a temp file, swaps in only on success
└────────┬────────┘
         ▼
   DuckDB store
   ├── core/       cleaned dimensions and facts
   ├── analytics/  pre-aggregated daily marts
   └── quality/    the audit trail of every run
         │
         ▼
┌─────────────────┐
│   metrics.py    │  ← the single source of every KPI
│   analytics.py  │
└────────┬────────┘
         ▼
    FastAPI ──► dashboard API, CSV exports, Tableau extract
```

**The store swap is atomic.** The pipeline builds into a temporary file and
renames it into place only after a successful run. A failed refresh leaves the
previous good store serving traffic — the platform degrades visibly rather than
going dark. API requests open a short-lived read-only connection, so an
in-flight request keeps reading the store it opened even while a new one is
being swapped in.

---

## API

All routes are under `/api`. Full schema at `/docs`.

### Service metadata

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Liveness. Does not touch the database. |
| `GET` | `/api/ready` | Readiness: store built and fresh. |
| `GET` | `/api/dimensions` | Every filterable department, facility, visit type, status. |
| `GET` | `/api/freshness` | When the data was last refreshed, and whether that is stale. |
| `GET` | `/api/bounds` | Earliest and latest date in the store. |

### Metrics

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/metrics/kpis` | Headline KPI cards with period-over-period change. |
| `GET` | `/api/metrics/trend` | Appointment and encounter trend at day/week/month grain. |
| `GET` | `/api/metrics/status-mix` | Completed / cancelled / no-show / open split. |
| `GET` | `/api/metrics/visit-type-mix` | Volume by visit type, with the outcome split within each. |
| `GET` | `/api/metrics/cancellation-reasons` | Reasons, split by patient / provider / facility. |
| `GET` | `/api/metrics/patient-flow` | Median, mean, p90, p95 wait and the 15-minute histogram. |
| `GET` | `/api/metrics/wait-trend` | Median and mean wait over time, with sample size per period. |
| `GET` | `/api/metrics/departments` | Per-department comparison with the ranking guard. |

### Data quality

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/quality/runs` | Recent pipeline runs, newest first. |
| `GET` | `/api/quality/latest` | The most recent run. |
| `GET` | `/api/quality/issues` | Findings for a run, grouped by category. |
| `GET` | `/api/quality/issue-categories` | Issue counts by category and severity. |
| `GET` | `/api/quality/row-counts` | Row counts per analytical table. |

### Exports

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/export/kpis.csv` | The KPI set, with definitions and sample sizes. |
| `GET` | `/api/export/departments.csv` | The department table, flattened. |
| `GET` | `/api/export/marts/{table}.csv` | Filtered daily mart extract. |

`{table}` is one of `appointments`, `encounters`, `capacity`, `flow`.

Every export carries an `X-Export-Filters` header describing the window and
selection it was generated under. A CSV that has lost its filter context is a
misleading artifact, so the context travels with the file.

### Admin

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/admin/refresh` | Rebuild the analytical store. |

**Disabled by default.** Set `HOA_ADMIN_ENABLED=true` to enable it. An
unauthenticated rebuild trigger on a reachable host is a denial-of-service
waiting to happen; rebuild offline with `make pipeline` instead.

### Filters

Every metrics and export route accepts the same filter query parameters:

| Parameter | Repeatable | Notes |
|---|---|---|
| `start_date` | no | Inclusive start (YYYY-MM-DD). Defaults to 90 days before `end_date`. |
| `end_date` | no | Inclusive end (YYYY-MM-DD). Defaults to today. |
| `department_id` | yes | |
| `visit_type` | yes | |
| `status` | yes | |
| `facility_id` | yes | |

Windows are capped at 1,200 days. An inverted or oversized range returns 422.

```bash
curl 'http://127.0.0.1:8000/api/metrics/kpis?start_date=2026-07-13&end_date=2026-10-10&department_id=DEPT-01'
```

---

## Security

- **No user input reaches SQL as text.** Every value is a bound parameter. The
  only strings concatenated into a query are fixed fragments the code authored
  itself, and column and table names come from allow-lists — the export table
  parameter is resolved against a fixed dict, so no request can name a table.
- **Errors do not leak internals.** An unexpected exception returns a generic
  body; the detail goes to the log. Set `HOA_EXPOSE_ERROR_DETAIL=true` only for
  local debugging.
- **No row-level data is logged.** The dataset is synthetic, but the pipeline
  treats it as if it were sensitive so the same code path is safe against a real,
  de-identified extract.
- **CORS is narrow by default** — only the local dev server origin.
- **Exports are row-capped** at 250,000 rows so one request cannot stream the
  entire store into memory.

---

## Configuration

All settings are read from environment variables prefixed `HOA_`, with working
defaults. See [`.env.example`](.env.example) for the full documented surface.

The ones worth knowing:

| Variable | Default | Notes |
|---|---|---|
| `HOA_DATABASE_PATH` | `data/processed/healthcare_ops.duckdb` | The analytical store. |
| `HOA_SYNTHETIC_SEED` | `20261010` | Fixed, so the dataset is reproducible. |
| `HOA_SYNTHETIC_DAYS` | `365` | Trailing days of history to generate. |
| `HOA_STALE_AFTER_HOURS` | `24` | Older than this and the UI shows "stale". |
| `HOA_FACILITY_TIMEZONE` | `Asia/Kolkata` | Timestamps are stored UTC, displayed here. |
| `HOA_ADMIN_ENABLED` | `false` | Enables `POST /api/admin/refresh`. |
| `HOA_EXPOSE_ERROR_DETAIL` | `false` | **Must stay false outside local dev.** |

---

## Data

All data is **synthetic and fabricated for demonstration**. No real patient,
staff member, department or facility is represented.

The generator is fully seeded: the same seed produces byte-identical CSVs and
therefore identical KPIs, which is what makes the reconciliation figures in the
KPI dictionary checkable. `data/raw/flawed/` holds a deliberately corrupted
fixture so the validation layer can be demonstrated failing without touching the
happy path.

See [`docs/DATA_DICTIONARY.md`](docs/DATA_DICTIONARY.md) for every table, its
grain and its columns.

---

## Documentation

| Document | Contents |
|---|---|
| [`docs/KPI_DICTIONARY.md`](docs/KPI_DICTIONARY.md) | Every metric: definition, denominator, exclusions, reconciliation. |
| [`docs/DATA_DICTIONARY.md`](docs/DATA_DICTIONARY.md) | Every table: grain, columns, types, exclusion rules. |

---

## Project layout

```
backend/
  app/
    config.py          settings, all env-driven with working defaults
    db.py              read-only connection per request
    filters.py         filter parsing and parameterised SQL predicates
    validation.py      row-level checks; errors quarantine, warnings report
    pipeline.py        ingestion + transformation; atomic store swap
    metrics.py         every KPI, computed once
    analytics.py       trends, mixes, department comparison, quality reads
    api/               thin HTTP layer; no business logic
  main.py              uvicorn entrypoint
  sql/                 schema, dimensions, facts, marts
scripts/
  generate_data.py     seeded synthetic dataset generator
docs/                  KPI and data dictionaries
data/
  raw/                 source CSVs (regenerated with `make data`)
  processed/           the DuckDB store (regenerated with `make pipeline`)
```

---

## Development

```bash
make test        # run the test suite
make check       # byte-compile every module
make reset       # delete the store; next pipeline rebuilds from scratch
make clean       # remove caches
```

### Reconciling a number

Because the seed is fixed, any published figure can be checked against the
store directly. For example, the cancellation rate:

```sql
SELECT
    SUM(cancelled_count) * 100.0 / SUM(terminal_count) AS cancellation_rate,
    SUM(terminal_count)                                AS denominator,
    SUM(cancelled_count)                               AS numerator
FROM analytics.mart_appointments_daily
WHERE metric_date BETWEEN DATE '2026-07-13' AND DATE '2026-10-10';
```

The API returns `numerator` and `denominator` on every rate metric precisely so
this check is possible without re-deriving the definition. See the KPI
dictionary for the full procedure.

"""Ingestion + transformation pipeline.

Responsibilities
    1. Read the raw CSVs (never modify them).
    2. Validate every table and produce a quality report.
    3. Write accepted rows to the ``staging`` layer.
    4. Execute the SQL models to build ``core`` and ``analytics``.
    5. Record the run in ``quality.data_quality_runs``.

Reliability contract (PRD §11)
    A failed refresh must NOT replace the last known-good analytics output.
    This is enforced structurally rather than by convention: the pipeline builds
    into a *temporary* DuckDB file and only swaps it into place once every model
    has succeeded. If anything raises, the temp file is discarded and the live
    store is left exactly as it was.

Observability
    Every run is recorded with row counts, status and a JSON error summary, so
    the Data Quality page can show both successful and failing runs.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from app.config import Settings, get_settings
from app.validation import ValidationResult, summarise_issues, validate_dataset

logger = logging.getLogger(__name__)

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

# Ordered list of model files. Order matters: schemas -> dimensions -> facts ->
# marts. Each file is idempotent so the whole set can be replayed safely.
SQL_MODEL_FILES: tuple[str, ...] = (
    "00_schemas.sql",
    "10_core_dimensions.sql",
    "20_core_facts.sql",
    "30_analytics_marts.sql",
)

RAW_TABLE_FILES: dict[str, str] = {
    "departments": "departments.csv",
    "appointments": "appointments.csv",
    "encounters": "encounters.csv",
    "capacity_daily": "capacity_daily.csv",
}

# Columns read from each CSV. Explicit lists (rather than inference) keep the
# contract stable if a source file gains an unexpected trailing column.
RAW_READ_COLUMNS: dict[str, list[str]] = {
    "departments": ["department_id", "department_name", "specialty_group", "facility_id"],
    "appointments": [
        "appointment_id", "facility_id", "department_id",
        "appointment_created_at", "scheduled_start_at", "status",
        "visit_type", "cancellation_reason_category",
    ],
    "encounters": [
        "encounter_id", "appointment_id", "facility_id", "department_id",
        "check_in_at", "service_start_at", "service_end_at", "encounter_status",
    ],
    "capacity_daily": [
        "date", "facility_id", "department_id",
        "available_slots", "staffed_slots", "occupied_slots",
    ],
}


class PipelineError(RuntimeError):
    """Raised when the pipeline cannot produce a trustworthy analytics store."""


@dataclass
class PipelineOutcome:
    """Summary of a completed pipeline run."""

    run_id: str
    status: str
    started_at: datetime
    completed_at: datetime
    duration_seconds: float
    input_rows: int
    accepted_rows: int
    rejected_rows: int
    error_summary: dict[str, Any]
    issues: list[dict[str, Any]]
    tables: dict[str, dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "duration_seconds": round(self.duration_seconds, 3),
            "input_rows": self.input_rows,
            "accepted_rows": self.accepted_rows,
            "rejected_rows": self.rejected_rows,
            "error_summary": self.error_summary,
            "issues": self.issues,
            "tables": self.tables,
        }


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def read_raw_tables(raw_dir: Path) -> dict[str, pd.DataFrame]:
    """Read the raw CSVs, preserving every row exactly as supplied.

    Everything is read as string so that malformed values survive to the
    validation layer instead of being silently coerced or dropped by the
    parser. Type coercion happens only after validation has reported on it.
    """
    tables: dict[str, pd.DataFrame] = {}
    for table, filename in RAW_TABLE_FILES.items():
        path = raw_dir / filename
        if not path.exists():
            raise PipelineError(
                f"Missing required input file: {path}. "
                "Run `make data` (or `python scripts/generate_data.py`) first."
            )
        frame = pd.read_csv(
            path,
            dtype=str,
            keep_default_na=False,
            na_values=[""],
            usecols=lambda c: c in RAW_READ_COLUMNS[table],
        )
        tables[table] = frame
        logger.info("read raw table", extra={"table": table, "rows": len(frame)})
    return tables


# ---------------------------------------------------------------------------
# Writing staging
# ---------------------------------------------------------------------------

def _write_staging(conn: duckdb.DuckDBPyConnection, results: dict[str, ValidationResult]) -> None:
    """Register accepted rows as staging tables.

    Empty frames are still created (with the correct columns) so downstream SQL
    never fails on a missing relation — an empty capacity input is a legitimate
    state, not an error.
    """
    for table, result in results.items():
        frame = result.accepted
        relation = f"staging.{table}"
        if frame.empty:
            # Create an empty table with the declared columns.
            columns = RAW_READ_COLUMNS[table]
            select_list = ", ".join(f"NULL::VARCHAR AS {c}" for c in columns)
            conn.execute(f"CREATE OR REPLACE TABLE {relation} AS SELECT {select_list} WHERE 1=0")
        else:
            conn.register(f"_staging_{table}", frame)
            conn.execute(f"CREATE OR REPLACE TABLE {relation} AS SELECT * FROM _staging_{table}")
            conn.unregister(f"_staging_{table}")
        logger.info(
            "wrote staging table",
            extra={"table": table, "rows": len(frame)},
        )


def _run_sql_models(conn: duckdb.DuckDBPyConnection) -> None:
    """Execute every SQL model file in order."""
    for filename in SQL_MODEL_FILES:
        path = SQL_DIR / filename
        if not path.exists():
            raise PipelineError(f"SQL model file not found: {path}")
        sql = path.read_text(encoding="utf-8")
        try:
            conn.execute(sql)
        except duckdb.Error as exc:
            raise PipelineError(f"SQL model {filename} failed: {exc}") from exc
        logger.info("executed sql model", extra={"model": filename})


def _record_run(
    conn: duckdb.DuckDBPyConnection,
    outcome: PipelineOutcome,
) -> None:
    """Persist the run in ``quality.data_quality_runs``."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS quality.data_quality_runs (
            run_id          VARCHAR PRIMARY KEY,
            started_at      TIMESTAMP,
            completed_at    TIMESTAMP,
            status          VARCHAR,
            input_rows      BIGINT,
            accepted_rows   BIGINT,
            rejected_rows   BIGINT,
            error_summary   VARCHAR
        )
        """
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO quality.data_quality_runs
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            outcome.run_id,
            outcome.started_at.replace(tzinfo=None),
            outcome.completed_at.replace(tzinfo=None),
            outcome.status,
            outcome.input_rows,
            outcome.accepted_rows,
            outcome.rejected_rows,
            json.dumps(outcome.error_summary),
        ],
    )


def _record_issues(conn: duckdb.DuckDBPyConnection, outcome: PipelineOutcome) -> None:
    """Persist individual findings so the UI can group by category and severity."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS quality.data_quality_issues (
            run_id       VARCHAR,
            check_id     VARCHAR,
            category     VARCHAR,
            severity     VARCHAR,
            table_name   VARCHAR,
            column_name  VARCHAR,
            row_count    BIGINT,
            message      VARCHAR,
            remediation  VARCHAR,
            sample_keys  VARCHAR
        )
        """
    )
    if not outcome.issues:
        return
    conn.executemany(
        """
        INSERT INTO quality.data_quality_issues
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            [
                outcome.run_id,
                issue["check_id"],
                issue["category"],
                issue["severity"],
                issue["table"],
                issue["column"],
                issue["row_count"],
                issue["message"],
                issue["remediation"],
                json.dumps(issue["sample_keys"]),
            ]
            for issue in outcome.issues
        ],
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _summarise(results: dict[str, ValidationResult]) -> tuple[int, int, int]:
    input_rows = sum(r.input_rows for r in results.values())
    accepted = sum(r.accepted_rows for r in results.values())
    rejected = sum(r.rejected_rows for r in results.values())
    return input_rows, accepted, rejected


def run_pipeline(
    settings: Settings | None = None,
    *,
    raw_dir: Path | None = None,
    database_path: Path | None = None,
    allow_rejected: bool = True,
) -> PipelineOutcome:
    """Run the full ingestion + transformation pipeline.

    Args:
        settings: Optional settings override (used heavily in tests).
        raw_dir: Optional raw-CSV directory override.
        database_path: Optional output database override.
        allow_rejected: When False, any quarantined row fails the run. Default
            True: quarantined rows are reported and the run still succeeds, so
            the platform degrades visibly rather than going dark.

    Returns:
        A :class:`PipelineOutcome` describing the run.

    Raises:
        PipelineError: If a required input is missing or a model fails. The
            live database is left untouched in that case.
    """
    settings = settings or get_settings()
    raw_dir = raw_dir or settings.raw_dir
    target = database_path or settings.database_file

    run_id = f"run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    started_at = datetime.now(timezone.utc)
    t0 = time.perf_counter()

    logger.info("pipeline starting", extra={"run_id": run_id, "raw_dir": str(raw_dir)})

    # --- 1 & 2: read and validate ------------------------------------------
    tables = read_raw_tables(raw_dir)
    results = validate_dataset(tables)

    input_rows, accepted_rows, rejected_rows = _summarise(results)
    error_summary = summarise_issues(results)

    logger.info(
        "validation complete",
        extra={
            "run_id": run_id,
            "input_rows": input_rows,
            "accepted_rows": accepted_rows,
            "rejected_rows": rejected_rows,
            "issues": error_summary["issue_count"],
        },
    )

    if not allow_rejected and rejected_rows > 0:
        raise PipelineError(
            f"Validation quarantined {rejected_rows:,} row(s); the run was "
            "configured to fail rather than proceed. See the data-quality report."
        )

    # --- 3 & 4: build into a temporary database, then swap -----------------
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_suffix(target.suffix + ".tmp")
    if temp_path.exists():
        temp_path.unlink()

    issues_flat = [issue.to_dict() for r in results.values() for issue in r.issues]

    try:
        conn = duckdb.connect(str(temp_path))
        try:
            # Schemas must exist before staging tables are written.
            conn.execute((SQL_DIR / "00_schemas.sql").read_text(encoding="utf-8"))
            _write_staging(conn, results)
            # Dimensions and facts depend on staging; run the remaining models.
            for filename in SQL_MODEL_FILES[1:]:
                sql = (SQL_DIR / filename).read_text(encoding="utf-8")
                try:
                    conn.execute(sql)
                except duckdb.Error as exc:
                    raise PipelineError(f"SQL model {filename} failed: {exc}") from exc
                logger.info("executed sql model", extra={"model": filename})

            completed_at = datetime.now(timezone.utc)
            outcome = PipelineOutcome(
                run_id=run_id,
                status="succeeded" if rejected_rows == 0 else "succeeded_with_warnings",
                started_at=started_at,
                completed_at=completed_at,
                duration_seconds=time.perf_counter() - t0,
                input_rows=input_rows,
                accepted_rows=accepted_rows,
                rejected_rows=rejected_rows,
                error_summary=error_summary,
                issues=issues_flat,
                tables={
                    name: {
                        "input_rows": r.input_rows,
                        "accepted_rows": r.accepted_rows,
                        "rejected_rows": r.rejected_rows,
                        "status": r.status,
                    }
                    for name, r in results.items()
                },
            )
            _record_run(conn, outcome)
            _record_issues(conn, outcome)
            conn.execute("CHECKPOINT")
        finally:
            conn.close()
    except Exception:
        # The live store is untouched; discard the partial build.
        if temp_path.exists():
            temp_path.unlink()
        logger.exception("pipeline failed; last known-good output retained",
                         extra={"run_id": run_id})
        raise

    # --- 5: atomic swap into place ----------------------------------------
    # os.replace is atomic within a filesystem, so a reader either sees the
    # whole previous database or the whole new one, never a partial state.
    os.replace(temp_path, target)
    wal = Path(str(temp_path) + ".wal")
    if wal.exists():
        wal.unlink()

    logger.info(
        "pipeline complete",
        extra={
            "run_id": run_id,
            "status": outcome.status,
            "duration_seconds": round(outcome.duration_seconds, 3),
            "database": str(target),
        },
    )
    return outcome


def rebuild_database(settings: Settings | None = None) -> PipelineOutcome:
    """Convenience wrapper: run the pipeline against the configured store."""
    return run_pipeline(settings)


def reset_database(settings: Settings | None = None) -> None:
    """Delete the local analytical store so the next run rebuilds from scratch."""
    settings = settings or get_settings()
    for path in (
        settings.database_file,
        Path(str(settings.database_file) + ".wal"),
        Path(str(settings.database_file) + ".tmp"),
    ):
        if path.exists():
            path.unlink()
            logger.info("removed", extra={"path": str(path)})


def copy_known_good(settings: Settings | None = None, destination: Path | None = None) -> Path:
    """Back up the current store (used by tests and manual recovery)."""
    settings = settings or get_settings()
    destination = destination or settings.database_file.with_suffix(".backup.duckdb")
    shutil.copy2(settings.database_file, destination)
    return destination

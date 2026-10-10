"""Response models.

These are declared for the OpenAPI schema and for the shape the frontend can
rely on. The analytical functions already return plain dicts, and re-validating
every row of a 34,000-point series through pydantic on each request would cost
more than it buys, so the routers return those dicts directly while the models
here document the contract.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str = Field(description="'ok' when the service is up.")
    database_available: bool = Field(description="Whether the analytical store file exists.")
    environment: str
    version: str


class ReadinessResponse(BaseModel):
    status: str = Field(description="'ready' when the store exists and has been built.")
    database_path: str
    pipeline_status: str | None = Field(
        default=None, description="Status of the most recent pipeline run."
    )
    last_refresh_at: str | None = None
    data_start_date: str | None = None
    data_end_date: str | None = None


class MetricModel(BaseModel):
    """A single KPI with the context needed to read it honestly."""

    key: str
    label: str
    value: float | None
    unit: str
    numerator: float | None = None
    denominator: float | None = None
    sample_size: int | None = None
    available: bool = True
    unavailable_reason: str | None = None
    definition: str = ""
    caveat: str | None = None
    comparison_value: float | None = None
    change_percent: float | None = None
    change_available: bool = True
    change_unavailable_reason: str | None = None


class KpiResponse(BaseModel):
    filters: dict[str, Any]
    metrics: list[MetricModel]


class FilterEcho(BaseModel):
    """The resolved filter set, echoed back so the UI can show what it queried."""

    start_date: str
    end_date: str
    department_ids: list[str] = []
    visit_types: list[str] = []
    statuses: list[str] = []
    facility_ids: list[str] = []
    comparison_start_date: str
    comparison_end_date: str


class RefreshRequest(BaseModel):
    allow_rejected: bool = Field(
        default=True,
        description=(
            "When false, any quarantined row fails the refresh. Default true: "
            "quarantined rows are reported and the refresh still succeeds, so "
            "the platform degrades visibly rather than going dark."
        ),
    )


class RefreshResponse(BaseModel):
    run_id: str
    status: str
    started_at: str
    completed_at: str
    duration_seconds: float
    input_rows: int
    accepted_rows: int
    rejected_rows: int
    issue_count: int
    tables: dict[str, Any] = {}
    message: str | None = None

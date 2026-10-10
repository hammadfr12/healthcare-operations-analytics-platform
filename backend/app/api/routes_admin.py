"""Admin routes: triggering a pipeline refresh.

This is the one endpoint that writes to the analytical store. It is gated
behind ``HOA_ADMIN_ENABLED`` (off by default) because an unauthenticated
rebuild trigger on a public host is a denial-of-service waiting to happen.

The pipeline writes to a temporary file and swaps it in on success, so a
refresh that fails leaves the previous good store serving traffic.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.dependencies import settings_dependency
from app.api.schemas import RefreshRequest, RefreshResponse
from app.config import Settings
from app.pipeline import PipelineError, rebuild_database

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/refresh", response_model=RefreshResponse, summary="Rebuild the analytical store")
def refresh(
    request: RefreshRequest | None = None,
    settings: Settings = Depends(settings_dependency),
) -> RefreshResponse:
    """Run the ingestion and transformation pipeline.

    Requires ``HOA_ADMIN_ENABLED=true``. Returns the run summary either way, so
    a failed run is visible in the response rather than only in the logs.
    """
    if not settings.admin_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "The refresh endpoint is disabled. Set HOA_ADMIN_ENABLED=true "
                "to enable it, or rebuild offline with `make pipeline`."
            ),
        )

    options = request or RefreshRequest()
    logger.info("admin refresh requested", extra={"allow_rejected": options.allow_rejected})

    try:
        outcome = rebuild_database(settings)
    except PipelineError as exc:
        # A rejected row when allow_rejected=False, or a schema failure. The
        # previous store is still in place and still serving.
        logger.error("admin refresh failed", extra={"error": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    payload = outcome.to_dict()
    return RefreshResponse(
        run_id=payload["run_id"],
        status=payload["status"],
        started_at=payload["started_at"],
        completed_at=payload["completed_at"],
        duration_seconds=payload["duration_seconds"],
        input_rows=payload["input_rows"],
        accepted_rows=payload["accepted_rows"],
        rejected_rows=payload["rejected_rows"],
        issue_count=len(payload.get("issues") or []),
        tables=payload.get("tables") or {},
        message=(
            f"Rebuilt in {payload['duration_seconds']:.2f}s. "
            f"{payload['accepted_rows']:,} rows accepted, "
            f"{payload['rejected_rows']:,} quarantined."
        ),
    )

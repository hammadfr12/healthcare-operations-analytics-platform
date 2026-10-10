"""FastAPI application factory.

Route layout::

    /api/health            liveness
    /api/ready             readiness (store built and fresh)
    /api/dimensions        filterable dimension values
    /api/freshness         data freshness and coverage
    /api/bounds            dataset date bounds
    /api/metrics/*         KPI cards, trend, mixes, department comparison
    /api/quality/*         pipeline runs, issues, row counts
    /api/export/*.csv      filtered CSV exports
    /api/admin/refresh     rebuild the store (disabled by default)

Exception handling is centralised here so error bodies have one shape and,
importantly, so an unexpected error never leaks a stack trace or a SQL string
to the caller unless ``HOA_EXPOSE_ERROR_DETAIL`` is explicitly turned on for
local debugging.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import routes_admin, routes_export, routes_meta, routes_metrics, routes_quality
from app.api.routes_meta import API_VERSION
from app.config import get_settings
from app.logging_config import configure_logging

logger = logging.getLogger(__name__)

API_PREFIX = "/api"


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="Healthcare Operations Analytics Platform",
        description=(
            "Operational analytics over appointment, encounter and capacity "
            "data. Every metric is computed once, in app.metrics, and read by "
            "the dashboard, the CSV exports and the Tableau extract alike."
        ),
        version=API_VERSION,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Export-Filters", "X-Export-Filename"],
    )

    # --- Request logging ----------------------------------------------------
    @app.middleware("http")
    async def log_requests(
        request: Request, call_next: Callable[[Request], Awaitable[JSONResponse]]
    ):
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.exception(
                "request failed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "elapsed_ms": round(elapsed_ms, 1),
                },
            )
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000
        # Query strings are logged, not request bodies: the query string is
        # the filter selection, which is what makes a slow request diagnosable.
        logger.info(
            "request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "query": str(request.url.query),
                "status_code": response.status_code,
                "elapsed_ms": round(elapsed_ms, 1),
            },
        )
        response.headers["X-Response-Time-Ms"] = f"{elapsed_ms:.1f}"
        return response

    # --- Error handling -----------------------------------------------------
    @app.exception_handler(Exception)
    async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        """Never leak internals. Log the detail, return a generic body."""
        logger.exception(
            "unhandled error",
            extra={"method": request.method, "path": request.url.path},
        )
        detail = str(exc) if settings.expose_error_detail else (
            "An unexpected error occurred while processing the request. "
            "The detail has been logged."
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": detail, "path": request.url.path},
        )

    # --- Routes -------------------------------------------------------------
    app.include_router(routes_meta.router, prefix=API_PREFIX)
    app.include_router(routes_metrics.router, prefix=f"{API_PREFIX}/metrics")
    app.include_router(routes_quality.router, prefix=f"{API_PREFIX}")
    app.include_router(routes_export.router, prefix=f"{API_PREFIX}")
    app.include_router(routes_admin.router, prefix=f"{API_PREFIX}")

    @app.get("/", include_in_schema=False)
    def root() -> dict[str, str]:
        return {
            "service": "Healthcare Operations Analytics Platform",
            "version": API_VERSION,
            "docs": "/docs",
            "api": API_PREFIX,
        }

    return app


app = create_app()

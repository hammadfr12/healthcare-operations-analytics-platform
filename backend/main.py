"""Uvicorn entrypoint.

Run the API with::

    uvicorn main:app --reload --port 8000

or, from the repository root, ``make api``.
"""

from __future__ import annotations

from app.api.app import app, create_app

__all__ = ["app", "create_app"]

if __name__ == "__main__":
    import uvicorn

    from app.config import get_settings

    settings = get_settings()
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.environment == "development",
        log_config=None,  # our structured formatter is installed in create_app
    )

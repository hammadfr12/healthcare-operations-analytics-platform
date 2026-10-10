"""Application configuration.

All settings are read from environment variables (prefix ``HOA_``) with working
defaults, so the demo runs with no ``.env`` file and no external credentials.
See ``.env.example`` at the repository root for the documented surface.
"""

from __future__ import annotations

import functools
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repository root: <root>/backend/app/config.py -> <root>
REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Runtime settings for the Healthcare Operations Analytics Platform."""

    model_config = SettingsConfigDict(
        env_prefix="HOA_",
        env_file=(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Runtime ------------------------------------------------------------
    environment: str = "development"
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"

    # --- Storage ------------------------------------------------------------
    database_path: str = "data/processed/healthcare_ops.duckdb"

    # --- Data directories ---------------------------------------------------
    data_dir: str = "data"
    raw_data_dir: str = "data/raw"
    flawed_data_dir: str = "data/raw/flawed"
    export_dir: str = "exports/tableau"

    # --- Synthetic data generation -----------------------------------------
    synthetic_seed: int = 20261010
    synthetic_days: int = 365
    synthetic_anchor_date: str = "2026-10-10"

    # --- Presentation -------------------------------------------------------
    facility_timezone: str = "Asia/Kolkata"
    stale_after_hours: int = 24

    # --- Admin --------------------------------------------------------------
    admin_enabled: bool = False
    """Enable POST /api/admin/refresh. Off by default: an unauthenticated
    rebuild trigger on a reachable host is a denial-of-service waiting to
    happen. Rebuild offline with ``make pipeline`` instead."""

    # --- Security -----------------------------------------------------------
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ]
    )
    expose_error_detail: bool = False

    # --- Validators ---------------------------------------------------------
    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """Allow ``HOA_CORS_ORIGINS=a,b,c`` as well as a JSON list."""
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                return value  # let pydantic parse the JSON form
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value

    @field_validator("facility_timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:  # pragma: no cover
            raise ValueError(f"Unknown IANA timezone: {value!r}") from exc
        return value

    # --- Derived paths ------------------------------------------------------
    def _resolve(self, relative: str) -> Path:
        path = Path(relative)
        return path if path.is_absolute() else (REPO_ROOT / path)

    @property
    def database_file(self) -> Path:
        return self._resolve(self.database_path)

    @property
    def raw_dir(self) -> Path:
        return self._resolve(self.raw_data_dir)

    @property
    def flawed_dir(self) -> Path:
        return self._resolve(self.flawed_data_dir)

    @property
    def export_path(self) -> Path:
        return self._resolve(self.export_dir)

    @property
    def processed_dir(self) -> Path:
        return self._resolve("data/processed")

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.facility_timezone)


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings singleton."""
    return Settings()

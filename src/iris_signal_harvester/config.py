"""Configuration: 12-factor, with a run-stamped config hash.

Behaviour comes from environment variables; domain knowledge comes from
versioned YAML. Every run records ``config_hash`` in ``harvest_run`` so any
output can be traced back to the exact configuration that produced it.

No secret ever has a default. A missing DSN fails loudly at startup rather than
silently falling back to something that happens to connect.
"""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings", "SourceName", "get_settings"]

SourceName = Literal["ratsdokumente", "oparl", "fixture"]

# A contact is a mailto: with an address, or an absolute http(s) URL with a host.
_UA_CONTACT = re.compile(r"mailto:[^\s@()<>]+@[^\s@()<>]+|https?://[^\s/()<>]+", re.IGNORECASE)


class Settings(BaseSettings):
    """Runtime configuration, read from the environment and an optional .env file."""

    model_config = SettingsConfigDict(
        env_prefix="IRIS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- source selection ---------------------------------------------------
    source: SourceName = Field(
        default="fixture",
        description="Which adapter to run. Defaults to 'fixture' so the demo needs no network.",
    )

    # --- database -----------------------------------------------------------
    db_dsn: SecretStr | None = Field(
        default=None,
        description="PostgreSQL DSN. Required for any command that writes; never committed.",
    )
    db_schema: str = "iris_staging"

    # --- configuration files ------------------------------------------------
    taxonomy_path: Path = Path("config/taxonomy.yaml")
    bodies_path: Path = Path("config/bodies.yaml")
    thresholds_path: Path = Path("config/thresholds.yaml")
    fixture_dir: Path = Path("tests/fixtures/cassettes")

    # --- politeness ---------------------------------------------------------
    rate_limit_rps: float = Field(default=1.0, gt=0, le=50)
    max_concurrent_bodies: int = Field(default=4, ge=1, le=32)
    request_timeout_s: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=4, ge=0, le=10)
    # The default contact is a deliberate placeholder (RFC 2606 example domain): a
    # deployment MUST set IRIS_USER_AGENT to an address that reaches its operator, since
    # that address is what lets a site owner contact us instead of silently blocking us.
    user_agent: str = "ProjectIris-Harvester/0.1 (+mailto:ops@example.org)"
    respect_robots: bool = True

    # --- incremental harvesting --------------------------------------------
    overlap_hours: int = Field(
        default=24,
        ge=0,
        description=(
            "How far before the watermark to re-request. Municipal systems back-date "
            "edits; without overlap those records are lost permanently. Content hashing "
            "makes the overlap free."
        ),
    )
    max_pages_per_body: int = Field(default=500, ge=1)
    zero_yield_pages_before_stop: int = Field(default=2, ge=1)

    # --- date sanity --------------------------------------------------------
    min_plausible_year: int = Field(
        default=1990,
        description=(
            "Dates before this are treated as malformed. Real OParl endpoints emit "
            "sentinel values such as '-0001-11-30T00:00:00+00:53'."
        ),
    )
    max_future_days: int = Field(default=400, ge=0)
    source_timezone: str = "Europe/Berlin"

    # --- geo ----------------------------------------------------------------
    municipality_tolerance_m: float = Field(default=2000.0, ge=0)

    # --- behaviour ----------------------------------------------------------
    strict: bool = Field(
        default=False,
        description="CI mode: any record-level rejection fails the run.",
    )
    enable_llm_rerank: bool = False
    log_level: str = "INFO"
    report_dir: Path = Path("reports")

    @field_validator("user_agent")
    @classmethod
    def _ua_must_be_contactable(cls, value: str) -> str:
        if not _UA_CONTACT.search(value):
            raise ValueError(
                "user_agent must carry a contact: a 'mailto:<address>' or an 'http(s)://<host>' "
                "URL — it is what lets an operator reach us instead of silently blocking us"
            )
        return value

    @field_validator("source_timezone")
    @classmethod
    def _timezone_must_exist(cls, value: str) -> str:
        """Fail at startup, not on the first naive date of a sweep."""
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
            raise ValueError(f"source_timezone {value!r} is not a known IANA zone: {exc}") from exc
        return value

    def require_dsn(self) -> str:
        """Return the DSN or fail loudly. Never returns a fallback."""
        if self.db_dsn is None:
            raise_missing = (
                "IRIS_DB_DSN is not set. This command writes to PostgreSQL and there is "
                "deliberately no default — set it in the environment or .env."
            )
            from iris_signal_harvester.errors import ConfigurationError

            raise ConfigurationError(raise_missing)
        return self.db_dsn.get_secret_value()

    def config_hash(self) -> str:
        """Stable hash of the non-secret configuration, stamped on every run."""
        payload = self.model_dump(mode="json", exclude={"db_dsn"})
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()

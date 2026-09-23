"""Central configuration for ServiceMesh.

Every tunable value is read from the environment so the same image can run in
local development, docker-compose and CI without code changes. Defaults are
chosen so that `pytest` works with zero configuration (SQLite + in-process
provider transport), while docker-compose overrides them to PostgreSQL + HTTP.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Application -----------------------------------------------------
    app_name: str = "ServiceMesh"
    environment: Literal["local", "docker", "test", "prod"] = "local"
    debug: bool = True
    allow_demo_external_records: bool = True

    # --- Database --------------------------------------------------------
    # SQLite default keeps the test suite dependency-free. docker-compose sets
    # DATABASE_URL to the PostgreSQL DSN.
    database_url: str = "sqlite+pysqlite:///./servicemesh.db"
    db_echo: bool = False

    # --- Security --------------------------------------------------------
    jwt_secret: str = Field(
        default="dev-only-insecure-secret-change-me-please-use-32-bytes-minimum",
        description="Overridden via JWT_SECRET in any non-local environment.",
    )
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 240

    # --- Provider service base URLs -------------------------------------
    marketplace_url: str = "http://marketplace:8101"
    manufacturer_url: str = "http://manufacturer:8102"
    warranty_url: str = "http://warranty:8103"
    service_centre_url: str = "http://service-centre:8104"
    parts_supplier_url: str = "http://parts-supplier:8105"

    # "http"   -> real network calls (docker-compose, production)
    # "inproc" -> httpx ASGITransport straight into the provider apps (tests)
    provider_transport: Literal["http", "inproc"] = "inproc"

    # --- Provider credentials -------------------------------------------
    # Each organization authenticates differently on purpose; the adapter for
    # each one knows which of these to present and in which header/param.
    marketplace_api_key: str = "mkt-dev-key"
    manufacturer_token: str = "oem-dev-token"
    warranty_client_id: str = "wty-client"
    warranty_client_secret: str = "wty-secret"
    service_centre_key: str = "svc-dev-key"
    supplier_api_key: str = "sup-dev-key"

    # --- Resilience defaults --------------------------------------------
    provider_timeout_seconds: float = 5.0
    default_max_retries: int = 3
    retry_base_delay_seconds: float = 0.2
    retry_max_delay_seconds: float = 5.0
    # Tests set this to 0 so exponential backoff does not slow the suite down.
    retry_delay_multiplier: float = 1.0

    # --- Event bus -------------------------------------------------------
    # "memory" -> in-process synchronous dispatch + DB persistence (always on)
    # "kafka"  -> additionally publish to Kafka/Redpanda
    event_backend: Literal["memory", "kafka"] = "memory"
    kafka_bootstrap_servers: str = "redpanda:9092"
    kafka_topic_prefix: str = "servicemesh"

    # --- ML / GenAI ------------------------------------------------------
    ml_model_path: str = "artifacts/provider_risk_model.joblib"
    ml_enabled: bool = True
    genai_enabled: bool = False
    genai_api_key: str | None = None

    # --- SLA -------------------------------------------------------------
    default_sla_hours: int = 72


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Used by tests that mutate environment variables."""
    get_settings.cache_clear()

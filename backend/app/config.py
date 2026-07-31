"""Runtime configuration.

Every tunable the agent depends on is here rather than at a call site. The
constitution's Technology Constraints forbid hardcoding a model name where it is
used, so `llm_model` is read from the environment and threaded through.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolved absolutely, not relative to the working directory. uvicorn is started
# from backend/, the data pipeline from the repo root, and pytest from either —
# a relative "./.env" silently resolves to a different file in each case, which
# presents as an unreachable LLM rather than as a missing config.
REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(ENV_FILE, ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- LLM ---------------------------------------------------------------
    openrouter_api_key: str = Field(default="", alias="OPENROUTER_API_KEY")
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1", alias="OPENROUTER_BASE_URL"
    )
    llm_model: str = Field(default="anthropic/claude-sonnet-5", alias="LLM_MODEL")
    llm_temperature: float = Field(default=0.0, alias="LLM_TEMPERATURE")

    # --- MongoDB -----------------------------------------------------------
    mongo_uri: str = Field(
        default="mongodb://procurement_app:procurement_app_pw@localhost:27017/procurement?authSource=procurement",
        alias="MONGO_URI",
    )
    mongo_admin_uri: str = Field(
        default="mongodb://root:root_pw@localhost:27017/?authSource=admin",
        alias="MONGO_ADMIN_URI",
    )
    mongo_db: str = Field(default="procurement", alias="MONGO_DB")
    mongo_collection: str = Field(default="purchase_orders", alias="MONGO_COLLECTION")
    vocabulary_collection: str = "field_vocabulary"

    # --- Answer bounds -----------------------------------------------------
    max_result_rows: int = Field(default=200, alias="MAX_RESULT_ROWS")  # FR-013
    max_repair_attempts: int = Field(default=3, alias="MAX_REPAIR_ATTEMPTS")  # FR-008
    # SC-004 sets 30s. Measured provider latency varies 3-21s *per call* and a
    # question makes three, so 30s cuts off answers that were about to succeed.
    # Raised to keep the assistant usable; the shortfall against SC-004 is
    # recorded in the README rather than hidden.
    question_deadline_s: int = Field(default=60, alias="QUESTION_DEADLINE_S")
    query_timeout_ms: int = Field(default=15_000, alias="QUERY_TIMEOUT_MS")  # FR-026

    # --- Entity grounding --------------------------------------------------
    fuzzy_match_threshold: int = Field(default=90, alias="FUZZY_MATCH_THRESHOLD")
    fuzzy_ambiguous_floor: int = Field(default=75, alias="FUZZY_AMBIGUOUS_FLOOR")

    # --- Data coverage (FR-015) --------------------------------------------
    data_coverage_start: date = Field(default=date(2012, 7, 1), alias="DATA_COVERAGE_START")
    data_coverage_end: date = Field(default=date(2015, 6, 30), alias="DATA_COVERAGE_END")

    # --- Server ------------------------------------------------------------
    cors_origins: str = Field(default="http://localhost:4200", alias="CORS_ORIGINS")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Location Resolution"
    app_env: str = "development"
    database_url: str = "mongodb://location:location@localhost:27017/?authSource=admin"
    mongodb_database: str = "ssl_location_optimization"
    google_maps_api_key: str | None = None
    google_maps_embed_api_key: str | None = None
    here_api_key: str | None = None
    longdo_api_key: str | None = None
    openai_api_key: str | None = None
    dbd_api_enabled: bool = True
    dbd_api_base_url: str = "https://openapi.dbd.go.th/api/v1"
    dbd_name_api_base_url: str = "https://api.egov.go.th"
    dbd_consumer_key: str | None = None
    dbd_consumer_secret: str | None = None
    dbd_agent_id: str | None = None
    registry_request_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    google_place_details_limit: int = Field(default=10, ge=1, le=15)
    ai_research_enabled: bool = True
    # MVP policy: maximize recall and reasoning quality; tune cost/latency later.
    ai_research_model: str = "gpt-5.6-luna"
    ai_research_timeout_seconds: float = Field(default=300.0, gt=0, le=600)
    ai_research_max_queries: int = Field(default=12, ge=1, le=30)
    ai_research_max_tool_calls: int = Field(default=8, ge=1, le=20)
    ai_research_max_rounds: int = Field(default=3, ge=2, le=3)
    initial_search_queries: int = Field(default=8, ge=5, le=8)
    additional_search_queries: int = Field(default=5, ge=3, le=5)
    ai_candidate_limit: int = Field(default=15, ge=10, le=15)
    ai_accept_confidence: float = Field(default=0.5, ge=0.1, le=1)
    ai_research_cache_ttl_seconds: int = Field(default=86_400, ge=60)
    osm_nominatim_base_url: str | None = None
    osm_user_agent: str | None = None
    google_request_timeout_seconds: float = Field(default=4.0, gt=0, le=30)
    google_max_retries: int = Field(default=1, ge=0, le=3)
    google_max_pages: int = Field(default=2, ge=1, le=3)
    provider_result_limit: int = Field(default=20, ge=1, le=20)
    provider_concurrency: int = Field(default=8, ge=1, le=32)
    provider_circuit_failure_threshold: int = Field(default=3, ge=1, le=20)
    provider_circuit_cooldown_seconds: int = Field(default=30, ge=1, le=600)
    longdo_rate_limit_per_minute: int = Field(default=60, ge=1, le=600)
    osm_rate_limit_per_second: float = Field(default=1.0, gt=0, le=100)
    osm_max_search_queries: int = Field(default=1, ge=1, le=10)
    require_all_providers_in_production: bool = True
    max_search_queries: int = Field(default=40, ge=2, le=60)
    cache_ttl_seconds: int = Field(default=3600, ge=10)
    cache_max_entries: int = Field(default=5000, ge=100, le=100_000)
    rate_limit_requests: int = Field(default=30, ge=1)
    rate_limit_window_seconds: int = Field(default=60, ge=1)
    rate_limit_max_clients: int = Field(default=10_000, ge=100, le=1_000_000)
    top_candidate_min_margin: float = Field(default=10.0, ge=0, le=100)
    signing_secret: str = "development-only-signing-secret"
    log_level: str = "INFO"

    @property
    def ai_research_configured(self) -> bool:
        return self.ai_research_enabled and bool(self.openai_api_key)

    @property
    def street_view_api_key(self) -> str | None:
        """Prefer a browser-restricted key, while keeping local POC setups working."""
        return self.google_maps_embed_api_key or self.google_maps_api_key


@lru_cache
def get_settings() -> Settings:
    return Settings()

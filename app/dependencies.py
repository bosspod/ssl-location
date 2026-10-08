from functools import lru_cache

from app.config import get_settings
from app.providers.base import MapProvider
from app.providers.google import GoogleMapsProvider
from app.providers.here import HereMapsProvider
from app.providers.longdo import LongdoMapsProvider
from app.providers.osm import OpenStreetMapProvider
from app.rate_limit import InMemoryRateLimiter
from app.schemas import LocationCandidate, ResearchStatus
from app.services.cache import MemoryTTLCache
from app.services.registry import BusinessRegistryService, DBDRegistryClient
from app.services.research import LocationResearcher, OpenAIResearchService
from app.services.resolution import LocationResolutionService


@lru_cache
def get_providers() -> tuple[MapProvider, ...]:
    settings = get_settings()
    providers: list[MapProvider] = []
    if settings.google_maps_api_key:
        providers.append(
            GoogleMapsProvider(
                settings.google_maps_api_key,
                settings.google_request_timeout_seconds,
                settings.google_max_retries,
                settings.provider_result_limit,
                settings.google_max_pages,
            )
        )
    if settings.here_api_key:
        providers.append(
            HereMapsProvider(
                settings.here_api_key,
                settings.google_request_timeout_seconds,
                settings.provider_result_limit,
                settings.google_max_retries,
            )
        )
    if settings.longdo_api_key:
        providers.append(
            LongdoMapsProvider(
                settings.longdo_api_key,
                settings.google_request_timeout_seconds,
                settings.provider_result_limit,
                settings.google_max_retries,
                settings.longdo_rate_limit_per_minute,
            )
        )
    if settings.osm_nominatim_base_url and settings.osm_user_agent:
        providers.append(
            OpenStreetMapProvider(
                settings.osm_nominatim_base_url,
                settings.osm_user_agent,
                settings.google_request_timeout_seconds,
                settings.provider_result_limit,
                settings.osm_rate_limit_per_second,
                settings.osm_max_search_queries,
            )
        )
    return tuple(providers)


cache: MemoryTTLCache[list[LocationCandidate]] = MemoryTTLCache(get_settings().cache_max_entries)
research_cache: MemoryTTLCache[ResearchStatus] = MemoryTTLCache(get_settings().cache_max_entries)
rate_limiter = InMemoryRateLimiter(get_settings())


@lru_cache
def get_researcher() -> LocationResearcher | None:
    settings = get_settings()
    if not settings.ai_research_configured or not settings.openai_api_key:
        return None
    return OpenAIResearchService(
        api_key=settings.openai_api_key,
        model=settings.ai_research_model,
        timeout_seconds=settings.ai_research_timeout_seconds,
        maximum_queries=settings.ai_research_max_queries,
        maximum_tool_calls=settings.ai_research_max_tool_calls,
        cache=research_cache,
        cache_ttl_seconds=settings.ai_research_cache_ttl_seconds,
    )


@lru_cache
def get_registry() -> BusinessRegistryService | None:
    settings = get_settings()
    dbd = (
        DBDRegistryClient(
            settings.dbd_api_base_url,
            settings.registry_request_timeout_seconds,
            name_api_base_url=settings.dbd_name_api_base_url,
            consumer_key=settings.dbd_consumer_key,
            consumer_secret=settings.dbd_consumer_secret,
            agent_id=settings.dbd_agent_id,
        )
        if settings.dbd_api_enabled
        else None
    )
    return BusinessRegistryService(dbd) if dbd else None


@lru_cache
def get_resolution_service() -> LocationResolutionService:
    return LocationResolutionService(
        get_settings(), list(get_providers()), cache, get_researcher(), get_registry()
    )

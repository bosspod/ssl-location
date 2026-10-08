import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from app.config import Settings, get_settings
from app.database import client, database, ensure_indexes, ping
from app.dependencies import get_providers, get_resolution_service
from app.routes import api, web

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def validate_runtime_settings(runtime: Settings) -> None:
    if runtime.app_env == "production" and runtime.signing_secret in {
        "development-only-signing-secret",
        "change-me-in-production",
    }:
        raise RuntimeError("SIGNING_SECRET must be configured in production")
    if bool(runtime.osm_nominatim_base_url) != bool(runtime.osm_user_agent):
        raise RuntimeError("OSM_NOMINATIM_BASE_URL and OSM_USER_AGENT must be configured together")
    if (
        runtime.app_env == "production"
        and runtime.osm_nominatim_base_url
        and "nominatim.openstreetmap.org" in runtime.osm_nominatim_base_url
    ):
        raise RuntimeError("Use a self-hosted or contracted Nominatim endpoint in production")
    if runtime.app_env == "production" and runtime.require_all_providers_in_production:
        missing = [
            name
            for name, configured in {
                "google": runtime.google_maps_api_key,
                "here": runtime.here_api_key,
                "longdo": runtime.longdo_api_key,
                "openstreetmap": runtime.osm_nominatim_base_url and runtime.osm_user_agent,
            }.items()
            if not configured
        ]
        if missing:
            raise RuntimeError(
                f"Production map providers are not fully configured: {', '.join(missing)}"
            )


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    validate_runtime_settings(settings)
    await ping(database)
    await ensure_indexes(database)
    yield
    for provider in get_providers():
        await provider.close()
    await get_resolution_service().close()
    await client.close()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(web.router)
app.include_router(api.router)


@app.middleware("http")
async def security_headers(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; object-src 'none'; frame-ancestors 'none'; "
        "frame-src https://www.google.com https://maps.google.com"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
    return response


@app.get("/health/live", include_in_schema=False)
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready", include_in_schema=False)
async def readiness() -> JSONResponse:
    try:
        await ping(database)
        return JSONResponse(
            {
                "status": "ready",
                "providers": [provider.name for provider in get_providers()],
                "aiResearch": settings.ai_research_configured,
            }
        )
    except Exception:
        return JSONResponse({"status": "not_ready"}, status_code=503)

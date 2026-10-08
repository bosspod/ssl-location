import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.config import get_settings
from app.database import MongoDatabase, get_db
from app.dependencies import get_researcher, get_resolution_service, rate_limiter
from app.schemas import ConfirmRequest, LocationCandidate, ResolveRequest
from app.services.research import LocationResearcher
from app.services.resolution import LocationResolutionService

router = APIRouter(tags=["web"])
templates = Jinja2Templates(directory="app/templates")
FALLBACK_AI_MODELS = ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol")


@router.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "default_ai_model": get_settings().ai_research_model,
        },
    )


@router.get("/ui/models")
async def available_ai_models(
    researcher: Annotated[LocationResearcher | None, Depends(get_researcher)],
) -> dict[str, object]:
    default_model = get_settings().ai_research_model
    models: list[str] = []
    source = "fallback"
    if researcher:
        try:
            models = await researcher.available_models()
            source = "openai"
        except Exception:
            models = []
    ordered = list(dict.fromkeys([default_model, *FALLBACK_AI_MODELS, *models]))
    return {"models": ordered, "defaultModel": default_model, "source": source}


@router.post("/ui/resolve", response_class=HTMLResponse)
async def resolve_partial(
    request: Request,
    input: Annotated[str, Form(min_length=1, max_length=500)],
    database: Annotated[MongoDatabase, Depends(get_db)],
    service: Annotated[LocationResolutionService, Depends(get_resolution_service)],
    ai_model: Annotated[str | None, Form(max_length=100)] = None,
) -> HTMLResponse:
    await rate_limiter.check(request)
    result = await service.resolve(
        ResolveRequest(
            input=input,
            ai_model=ai_model,
        ),
        database,
    )
    return templates.TemplateResponse(
        request=request,
        name="partials/results.html",
        context={
            "result": result,
            "street_view_api_key": get_settings().street_view_api_key,
        },
    )


@router.post("/ui/resolve-stream")
async def resolve_stream(
    request: Request,
    input: Annotated[str, Form(min_length=1, max_length=500)],
    database: Annotated[MongoDatabase, Depends(get_db)],
    service: Annotated[LocationResolutionService, Depends(get_resolution_service)],
    ai_model: Annotated[str | None, Form(max_length=100)] = None,
) -> StreamingResponse:
    await rate_limiter.check(request)
    queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()

    async def report(event: dict[str, object]) -> None:
        await queue.put(event)

    async def run_resolution() -> None:
        try:
            result = await service.resolve(
                ResolveRequest(
                    input=input,
                    ai_model=ai_model,
                ),
                database,
                progress=report,
            )
            template = templates.get_template("partials/results.html")
            html = template.render(
                result=result,
                request=request,
                street_view_api_key=get_settings().street_view_api_key,
            )
            await queue.put({"stage": "result", "html": html})
        except Exception as exc:
            await queue.put(
                {
                    "stage": "error",
                    "title": "การค้นหาไม่สำเร็จ",
                    "detail": type(exc).__name__,
                }
            )
        finally:
            await queue.put(None)

    task = asyncio.create_task(run_resolution())

    async def events() -> AsyncIterator[str]:
        try:
            while (event := await queue.get()) is not None:
                yield json.dumps(event, ensure_ascii=False, default=str) + "\n"
        finally:
            task.cancel()

    return StreamingResponse(
        events(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/ui/confirm", response_class=HTMLResponse)
async def confirm_partial(
    request: Request,
    raw_input: Annotated[str, Form(min_length=1, max_length=500)],
    candidate_json: Annotated[str, Form()],
    database: Annotated[MongoDatabase, Depends(get_db)],
    service: Annotated[LocationResolutionService, Depends(get_resolution_service)],
) -> HTMLResponse:
    await rate_limiter.check(request)
    try:
        candidate = LocationCandidate.model_validate(json.loads(candidate_json))
        result = await service.confirm(
            ConfirmRequest(raw_input=raw_input, candidate=candidate), database
        )
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid confirmation candidate") from exc
    return templates.TemplateResponse(
        request=request, name="partials/confirmed.html", context={"confirmation": result}
    )

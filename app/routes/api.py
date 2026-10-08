from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from app.database import MongoDatabase, get_db
from app.dependencies import get_resolution_service, rate_limiter
from app.schemas import ConfirmRequest, ConfirmResponse, ResolveRequest, ResolveResponse
from app.services.resolution import LocationResolutionService

router = APIRouter(prefix="/api/location", tags=["location"])


@router.post("/resolve", response_model=ResolveResponse, response_model_by_alias=True)
async def resolve_location(
    payload: ResolveRequest,
    request: Request,
    database: Annotated[MongoDatabase, Depends(get_db)],
    service: Annotated[LocationResolutionService, Depends(get_resolution_service)],
) -> ResolveResponse:
    await rate_limiter.check(request)
    return await service.resolve(payload, database)


@router.post("/confirm", response_model=ConfirmResponse, response_model_by_alias=True)
async def confirm_location(
    payload: ConfirmRequest,
    request: Request,
    database: Annotated[MongoDatabase, Depends(get_db)],
    service: Annotated[LocationResolutionService, Depends(get_resolution_service)],
) -> ConfirmResponse:
    await rate_limiter.check(request)
    try:
        return await service.confirm(payload, database)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

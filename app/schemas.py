from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def to_camel(value: str) -> str:
    first, *rest = value.split("_")
    return first + "".join(word.capitalize() for word in rest)


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)


class InputType(StrEnum):
    PLACE = "PLACE"
    ADDRESS = "ADDRESS"
    PLACE_WITH_PHONE = "PLACE_WITH_PHONE"
    FULL_ADDRESS = "FULL_ADDRESS"
    PARTIAL_ADDRESS = "PARTIAL_ADDRESS"
    PHONE = "PHONE"
    UNKNOWN = "UNKNOWN"


class ConfidenceStatus(StrEnum):
    EXACT = "EXACT"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    CONFLICT = "CONFLICT"
    NO_RESULT = "NO_RESULT"


class ParsedLocation(ApiModel):
    raw_input: str
    normalized_input: str
    place_name: str | None = None
    company_name: str | None = None
    branch_name: str | None = None
    site_name: str | None = None
    area_context: str | None = None
    aliases: list[str] = Field(default_factory=list)
    customer_name: str | None = None
    phone: str | None = None
    house_number: str | None = None
    moo: str | None = None
    village: str | None = None
    building: str | None = None
    soi: str | None = None
    road: str | None = None
    subdistrict: str | None = None
    district: str | None = None
    province: str | None = None
    postal_code: str | None = None
    landmark: str | None = None
    registration_number: str | None = None
    registered_address: str | None = None
    input_type: InputType = InputType.UNKNOWN
    entity_confidence: dict[str, float] = Field(default_factory=dict, exclude=True)


class GeographicContext(ApiModel):
    previous_location: tuple[float, float] | None = None
    next_location: tuple[float, float] | None = None
    depot_location: tuple[float, float] | None = None
    province: str | None = None
    delivery_zone: str | None = None


class ResolveRequest(ApiModel):
    input: str = Field(min_length=1, max_length=500)
    context: GeographicContext | None = None
    ai_model: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    @field_validator("input")
    @classmethod
    def reject_control_characters(cls, value: str) -> str:
        if any(ord(ch) < 32 and ch not in "\n\r\t" for ch in value):
            raise ValueError("input contains unsupported control characters")
        if not value.strip():
            raise ValueError("input must not be blank")
        return value


class LocationCandidate(ApiModel):
    candidate_id: str | None = None
    provider: str
    provider_place_id: str | None = None
    name: str | None = None
    address: str | None = None
    phone: str | None = None
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    house_number: str | None = None
    moo: str | None = None
    village: str | None = None
    building: str | None = None
    soi: str | None = None
    road: str | None = None
    subdistrict: str | None = None
    district: str | None = None
    province: str | None = None
    postal_code: str | None = None
    category: str | None = None
    business_status: str | None = None
    website: str | None = None
    source_query: str
    raw: dict[str, Any] | None = Field(default=None, exclude=True)
    sources: list[str] = Field(default_factory=list)
    provider_place_ids: dict[str, str] = Field(default_factory=dict)
    score: float = 0
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    verification_status: str | None = None
    confirmation_token: str | None = None
    # AI-first result contract. Legacy name/address/score fields remain for UI/API
    # compatibility while these fields expose the requested resolution semantics.
    normalized_address: str | None = None
    matched_fields: list[str] = Field(default_factory=list)
    confidence_score: float = Field(default=0, ge=0, le=1)
    reason: str | None = None
    place_name: str | None = None
    location_role: str | None = None
    ai_verdict: str | None = None
    contradictions: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)

    @field_validator("sources", mode="after")
    @classmethod
    def unique_sources(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


class ProviderStatus(ApiModel):
    provider: str
    status: str
    latency_ms: float
    error: str | None = None
    cached: bool = False


class ResearchEvidence(ApiModel):
    title: str
    url: str


class DBDResearchStatus(ApiModel):
    status: str = "skipped"
    reason: str = "ยังไม่มีชื่อบริษัทหรือเลขทะเบียนเพียงพอสำหรับค้น DBD Open API"
    queries: list[str] = Field(default_factory=list)
    company_name: str | None = None
    registration_number: str | None = None
    registered_address: str | None = None
    evidence: list[ResearchEvidence] = Field(default_factory=list)


class ResearchStatus(ApiModel):
    status: str = "not_needed"
    query_hints: list[str] = Field(default_factory=list)
    evidence: list[ResearchEvidence] = Field(default_factory=list)
    latency_ms: float = 0
    error: str | None = None
    cached: bool = False
    rounds: int = 0
    exhausted: bool = False
    summary: str | None = None
    dbd: DBDResearchStatus = Field(default_factory=DBDResearchStatus)


class MapPreview(ApiModel):
    """A coordinate suitable for map inspection, without claiming an entity match."""

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    address: str | None = None
    name: str | None = None
    postal_code: str | None = None
    provider: str


class ResolveResponse(ApiModel):
    request_id: str
    query: str
    parsed: ParsedLocation
    status: ConfidenceStatus
    best_match: LocationCandidate | None
    alternatives: list[LocationCandidate]
    map_preview: MapPreview | None = None
    providers: list[ProviderStatus]
    research: ResearchStatus = Field(default_factory=ResearchStatus)
    processing_time_ms: float = Field(default=0, ge=0)


class ConfirmRequest(ApiModel):
    raw_input: str = Field(min_length=1, max_length=500)
    candidate: LocationCandidate


class ConfirmResponse(ApiModel):
    location_id: str
    verification_method: str
    message: str

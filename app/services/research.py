from __future__ import annotations

import json
import re
import time
from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.schemas import (
    DBDResearchStatus,
    GeographicContext,
    InputType,
    LocationCandidate,
    ParsedLocation,
    ResearchEvidence,
    ResearchStatus,
)
from app.services.cache import Cache


class ResearchHints(BaseModel):
    queries: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    summary: str = ""


class AgentSearchPlan(BaseModel):
    parsed: ParsedLocation
    queries: list[str] = Field(default_factory=list)
    summary: str = ""
    evidence: list[ResearchEvidence] = Field(default_factory=list)
    company_name_evidence: list[ResearchEvidence] = Field(default_factory=list)
    dbd: DBDResearchStatus = Field(default_factory=DBDResearchStatus)


class CandidateEvaluation(BaseModel):
    candidate_id: str
    verdict: str = Field(pattern=r"^(MATCH|POSSIBLE|REJECT)$")
    matched_fields: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    reason: str


class AgentResolution(BaseModel):
    evaluations: list[CandidateEvaluation] = Field(default_factory=list)
    additional_queries: list[str] = Field(default_factory=list)
    exhausted: bool = False
    summary: str = ""
    evidence: list[ResearchEvidence] = Field(default_factory=list)


class LocationResearcher(ABC):
    @staticmethod
    def is_eligible(parsed: ParsedLocation) -> bool:
        return bool(parsed.place_name and parsed.input_type.value in {"PLACE", "PLACE_WITH_PHONE"})

    @abstractmethod
    async def research(
        self,
        parsed: ParsedLocation,
        existing_queries: list[str],
        candidates: list[LocationCandidate],
    ) -> ResearchStatus: ...

    async def plan(
        self,
        raw_input: str,
        fallback: ParsedLocation,
        context: GeographicContext | None,
    ) -> AgentSearchPlan | None:
        return None

    async def resolve_candidates(
        self,
        raw_input: str,
        parsed: ParsedLocation,
        candidates: list[LocationCandidate],
        attempted_queries: list[str],
        round_number: int,
    ) -> AgentResolution | None:
        return None

    def with_model(self, model: str) -> LocationResearcher:
        return self

    async def available_models(self) -> list[str]:
        return []

    async def close(self) -> None:
        return None


class OpenAIResearchService(LocationResearcher):
    """AI-first planner and entity resolver; coordinates always come from providers."""

    endpoint = "https://api.openai.com/v1/responses"
    models_endpoint = "https://api.openai.com/v1/models"
    _phone_pattern = re.compile(r"(?<!\d)(?:\+?66|0)\d{8,10}(?!\d)")
    _coordinate_pattern = re.compile(
        r"(?:latitude|longitude|lat\s*[:=]|lng\s*[:=]|lon\s*[:=]|"
        r"-?\d{1,2}\.\d{4,}\s*[,/]\s*-?\d{2,3}\.\d{4,})",
        re.IGNORECASE,
    )
    _registration_pattern = re.compile(r"(?<!\d)\d{13}(?!\d)")
    _registry_query_pattern = re.compile(
        r"\b(?:dbd|datawarehouse|dataforthai|vat)\b|กรมสรรพากร", re.IGNORECASE
    )

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: float,
        maximum_queries: int,
        maximum_tool_calls: int,
        cache: Cache[ResearchStatus],
        cache_ttl_seconds: int,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model = model
        self.maximum_queries = maximum_queries
        self.maximum_tool_calls = maximum_tool_calls
        self.cache = cache
        self.cache_ttl_seconds = cache_ttl_seconds
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def with_model(self, model: str) -> LocationResearcher:
        if model == self.model:
            return self
        return OpenAIResearchService(
            api_key="",
            model=model,
            timeout_seconds=1,
            maximum_queries=self.maximum_queries,
            maximum_tool_calls=self.maximum_tool_calls,
            cache=self.cache,
            cache_ttl_seconds=self.cache_ttl_seconds,
            client=self.client,
        )

    async def available_models(self) -> list[str]:
        response = await self.client.get(self.models_endpoint)
        response.raise_for_status()
        data = response.json().get("data", [])
        return sorted(
            {
                str(item["id"])
                for item in data
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
        )

    async def research(
        self,
        parsed: ParsedLocation,
        existing_queries: list[str],
        candidates: list[LocationCandidate],
    ) -> ResearchStatus:
        public_context = self._public_context(parsed, candidates)
        if public_context is None:
            return ResearchStatus(status="not_eligible")
        cache_key = f"openai-research:{self.model}:{public_context.casefold()}"
        cached = await self.cache.get(cache_key)
        if cached is not None:
            return cached.model_copy(update={"cached": True, "latency_ms": 0})

        started = time.perf_counter()
        response = await self.client.post(self.endpoint, json=self._payload(public_context))
        response.raise_for_status()
        data = response.json()
        hints = self._parse_hints(data)
        evidence = self._extract_evidence(data, limit=20)
        known = {query.casefold() for query in existing_queries}
        query_hints: list[str] = []
        if evidence:
            for query in [*hints.queries, *hints.aliases]:
                cleaned = self._safe_query(query)
                if cleaned and cleaned.casefold() not in known:
                    known.add(cleaned.casefold())
                    query_hints.append(cleaned)
                if len(query_hints) >= self.maximum_queries:
                    break
        result = ResearchStatus(
            status="success" if query_hints else "no_hints",
            query_hints=query_hints,
            evidence=evidence,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        await self.cache.set(cache_key, result, self.cache_ttl_seconds)
        return result

    async def plan(
        self,
        raw_input: str,
        fallback: ParsedLocation,
        context: GeographicContext | None,
    ) -> AgentSearchPlan | None:
        data = await self._request(self._plan_payload(raw_input, fallback, context))
        payload = self._parse_json_output(data)
        evidence = self._extract_evidence(data, limit=40)
        parsed_values = payload.get("parsed") or {}
        company_name_evidence = self._company_name_evidence(
            payload.get("company_name_source_urls"), evidence
        )
        supplied_registration = str(parsed_values.get("registration_number") or "").strip()
        discovered_registration = (
            supplied_registration
            if company_name_evidence and re.fullmatch(r"\d{13}", supplied_registration)
            else self._registration_from_evidence(evidence)
        )
        parsed_values.update(
            {
                "raw_input": raw_input,
                "normalized_input": parsed_values.get("normalized_input")
                or fallback.normalized_input,
                "input_type": parsed_values.get("input_type") or fallback.input_type,
                # A web-discovered ID is only a lead. The registry phase still
                # has to verify both the 13-digit ID and the company identity.
                "registration_number": self._extract_registration_number(raw_input)
                or discovered_registration,
                "registered_address": None,
            }
        )
        parsed_values = self._guard_unverified_web_identity(
            parsed_values,
            fallback,
            raw_input,
            bool(company_name_evidence),
        )
        parsed_values = self._guard_unverified_acronym_expansion(
            parsed_values,
            raw_input,
            bool(company_name_evidence),
        )
        parsed_values["entity_confidence"] = {
            field: confidence
            for field, confidence in fallback.entity_confidence.items()
            if self._normalized_entity(parsed_values.get(field))
            == self._normalized_entity(getattr(fallback, field, None))
        }
        parsed = ParsedLocation.model_validate(parsed_values)
        non_registry_queries = [
            query
            for query in payload.get("queries") or []
            if not self._registry_query_pattern.search(str(query))
        ]
        queries = self._unique_safe_queries(non_registry_queries, allow_phone=True)
        return AgentSearchPlan(
            parsed=parsed,
            queries=queries,
            summary=str(payload.get("summary") or ""),
            evidence=evidence,
            company_name_evidence=company_name_evidence,
        )

    async def resolve_candidates(
        self,
        raw_input: str,
        parsed: ParsedLocation,
        candidates: list[LocationCandidate],
        attempted_queries: list[str],
        round_number: int,
    ) -> AgentResolution | None:
        data = await self._request(
            self._resolution_payload(raw_input, parsed, candidates, attempted_queries, round_number)
        )
        payload = self._parse_json_output(data)
        candidate_ids = {candidate.candidate_id for candidate in candidates}
        evaluations = [
            CandidateEvaluation.model_validate(item)
            for item in payload.get("evaluations") or []
            if isinstance(item, dict) and item.get("candidate_id") in candidate_ids
        ]
        return AgentResolution(
            evaluations=evaluations,
            additional_queries=self._unique_safe_queries(
                payload.get("additional_queries") or [], allow_phone=True
            ),
            exhausted=bool(payload.get("exhausted", False)),
            summary=str(payload.get("summary") or ""),
            evidence=self._extract_evidence(data),
        )

    async def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self.client.post(self.endpoint, json=payload)
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    def _plan_payload(
        self,
        raw_input: str,
        fallback: ParsedLocation,
        context: GeographicContext | None,
    ) -> dict[str, Any]:
        nullable_string: dict[str, Any] = {"type": ["string", "null"]}
        parsed_properties: dict[str, Any] = {
            key: nullable_string
            for key in (
                "place_name",
                "company_name",
                "branch_name",
                "site_name",
                "area_context",
                "customer_name",
                "phone",
                "house_number",
                "moo",
                "village",
                "building",
                "soi",
                "road",
                "subdistrict",
                "district",
                "province",
                "postal_code",
                "landmark",
                "registration_number",
            )
        }
        parsed_properties.update(
            {
                "normalized_input": {"type": "string"},
                "aliases": {"type": "array", "items": {"type": "string"}},
                "input_type": {"type": "string", "enum": [item.value for item in InputType]},
            }
        )
        schema = {
            "type": "object",
            "properties": {
                "parsed": {
                    "type": "object",
                    "properties": parsed_properties,
                    "required": list(parsed_properties),
                    "additionalProperties": False,
                },
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": self.maximum_queries,
                },
                "summary": {"type": "string"},
                "company_name_source_urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 5,
                },
            },
            "required": ["parsed", "queries", "summary", "company_name_source_urls"],
            "additionalProperties": False,
        }
        location_context = context.model_dump(mode="json") if context else None
        return self._agent_payload(
            schema,
            "location_search_plan",
            (
                "คุณคือ Senior Location Intelligence Agent สำหรับงานขนส่งประเทศไทย เป้าหมายสูงสุด"
                "คือหา physical location ที่ truck เข้าได้ถูกแห่งจาก raw data ที่สกปรก โดย Accuracy "
                "สำคัญกว่า cost และ speed วิเคราะห์ข้อความทั้งก้อนโดยไม่สมมติว่า format ถูกต้อง "
                "แยกชื่อร้าน บริษัทแม่ สาขา site/warehouse เบอร์ และทุกส่วนที่อยู่ ลบคำซ้ำ แก้เว้นวรรค "
                "คำย่อ คำสะกด ไทย/อังกฤษ และ transliteration แต่รักษา identifier สำคัญไว้ ค้นเว็บเพื่อ"
                "หา official name, alias, โทรศัพท์, ที่อยู่สำนักงาน/ที่ตั้งปฏิบัติการสาธารณะ และบริบท"
                "พื้นที่ สร้าง initial query ที่ precision สูง 5-8 คำค้น ครอบคลุมโทรศัพท์ ชื่อ บริษัท ชื่อ+"
                "ตำบล/จังหวัด/ถนน ที่อยู่บางส่วน และ warehouse/plant/site/branch/depot เมื่อเหมาะสม "
                "ห้ามใช้คำ generic เดี่ยว เช่น จังหวัด ชื่อย่าน หรืออักษรย่อบริษัทเพียงอย่างเดียว "
                "แต่ละ query ต้องยาวไม่เกิน 120 ตัวอักษร ใช้เฉพาะภาษาไทย/อังกฤษ/ตัวเลข เป็นคำค้นล้วน "
                "ห้ามมีคำอธิบาย คำถาม Markdown หรือผสมภาษาอื่น อย่า exact-match ข้อความทั้งก้อนอย่าง"
                "เดียว และห้ามสร้างพิกัดเอง "
                "อย่าตีความความสัมพันธ์ของ token จากลำดับคำ เครื่องหมายคั่น หรือความโด่งดังของสถานที่"
                "เพียงอย่างเดียว ให้สร้าง hypothesis ที่เป็นไปได้สำหรับ organization, trade name, branch, "
                "operational site, address และ area/landmark แล้วเลือกชนิดข้อมูลจากหลักฐานใน raw หรือ"
                "แหล่งภายนอกที่สอดคล้องกัน หากยังแยกไม่ได้ให้เก็บค่าเป็น alias/area_context และสะท้อน"
                "ความกำกวมใน summary แทนการบังคับเลือก อักษรย่อและคำสะกดทีละตัวต้องรักษาลำดับอักษร "
                "ลองรูปแบบเว้นวรรค จุด ไทย/อังกฤษ และ transliteration โดยห้ามขยายเป็นชื่อองค์กรเต็มจนกว่า"
                "จะมีหลักฐานเชื่อมโดยตรง ให้ค้นและเปรียบเทียบความเป็นไปได้มากกว่าหนึ่ง entity เมื่อกำกวม "
                "site_name ใช้เมื่อมีหลักฐานว่าเป็นชื่อ/รหัสจุดปฏิบัติการ ส่วนชื่อพื้นที่ ย่าน เขต เมือง "
                "จังหวัด นิคม หรือ landmark ให้เป็น area_context เว้นแต่มีหลักฐานว่าเป็นชื่อ site จริง "
                "site label อาจเป็นชื่อภายในและไม่จำเป็นต้องตรงชื่อ POI บนแผนที่ ให้พิจารณาบริบทเจ้าของ "
                "ข้อมูลด้วย ขั้นตอนนี้คือ Web Search สำหรับสร้าง Search Strategy แปลงชื่อบริษัท และหาเลข"
                "ทะเบียน 13 หลัก ต้องทำให้เสร็จก่อนขั้น Registry เมื่อพบ company_name ให้ค้นเว็บเพิ่มด้วย"
                "ชื่อเต็มคู่กับคำว่า เลขทะเบียน และ registration number และตรวจหน้า terms/legal/contact "
                "ของเว็บไซต์บริษัทก่อนสรุป หากยังไม่พบจึงใช้ public business directory หาเลขตั้งต้นได้ "
                "ห้ามค้นหรือใช้ข้อมูลจาก DBD, DBD DataWarehouse, VAT หรือกรมสรรพากรในขั้นตอนนี้ "
                "และห้ามใส่คำเหล่านี้ "
                "หรือ site:... ใน queries หากแปลงชื่อย่อ/ชื่อค้าเป็น company_name ให้ใช้หลักฐานจากเว็บไซต์ "
                "บริษัท เว็บไซต์เจ้าของแบรนด์ หรือ public business source ที่ไม่ใช่ Registry และคืน URL "
                "ที่รองรับชื่อนั้นใน company_name_source_urls หากยังไม่มีหลักฐานเชื่อมชื่อโดยตรง ให้คงชื่อ "
                "เดิม/อักษรย่อไว้และคืน company_name_source_urls เป็น [] ห้ามเดาชื่อกฎหมายหรือเลขทะเบียน "
                "registration_number ต้องเป็นเลข 13 หลักที่มีหลักฐานเว็บรองรับ หากไม่มีให้คืน null "
                "แหล่ง public business directory ใช้ได้เฉพาะหาเลขตั้งต้น เพราะ Backend จะตรวจชื่อและเลข"
                "กับ DBD Open API ซ้ำก่อนยอมรับ ห้ามใช้ VAT หรือข้อมูลกรมสรรพากร "
                "Backend จะนำ company_name, aliases และเลขทะเบียน 13 หลักที่ได้"
                "ไปตรวจสอบกับ DBD ในขั้นถัดไป โดยไม่เรียก VAT"
            ),
            json.dumps(
                {
                    "raw_input": raw_input,
                    "deterministic_fallback": fallback.model_dump(mode="json"),
                    "route_context": location_context,
                },
                ensure_ascii=False,
            ),
        )

    def _resolution_payload(
        self,
        raw_input: str,
        parsed: ParsedLocation,
        candidates: list[LocationCandidate],
        attempted_queries: list[str],
        round_number: int,
    ) -> dict[str, Any]:
        schema = {
            "type": "object",
            "properties": {
                "evaluations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "candidate_id": {"type": "string"},
                            "verdict": {
                                "type": "string",
                                "enum": ["MATCH", "POSSIBLE", "REJECT"],
                            },
                            "matched_fields": {"type": "array", "items": {"type": "string"}},
                            "contradictions": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "missing_evidence": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "reason": {"type": "string"},
                        },
                        "required": [
                            "candidate_id",
                            "verdict",
                            "matched_fields",
                            "contradictions",
                            "missing_evidence",
                            "reason",
                        ],
                        "additionalProperties": False,
                    },
                },
                "additional_queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": self.maximum_queries,
                },
                "exhausted": {"type": "boolean"},
                "summary": {"type": "string"},
            },
            "required": ["evaluations", "additional_queries", "exhausted", "summary"],
            "additionalProperties": False,
        }
        public_candidates = [
            candidate.model_dump(
                mode="json",
                exclude={"raw", "confirmation_token", "score_breakdown"},
            )
            for candidate in candidates
        ]
        return self._agent_payload(
            schema,
            "location_entity_resolution",
            (
                "คุณคือ Senior Entity Resolution Judge สำหรับสถานที่ขนส่ง เทียบทุก candidate กับ raw "
                "data พร้อมกันทั้งชื่อ alias บริษัทแม่ สาขา เบอร์ บ้านเลขที่ ถนน ตำบล อำเภอ จังหวัด "
                "รหัสไปรษณีย์ ประเภท site และบริบทพื้นที่ ใช้ latitude/longitude ที่ Map Provider ส่งมา"
                "เพื่อดู candidate ที่พิกัดเดียวกัน/ใกล้กันและ consensus ข้าม provider หากข้อมูลสำคัญ"
                "และพิกัดสอดคล้องกันให้ verdict=MATCH ได้ คืน candidate_id เดิม, verdict, matched_fields, "
                "contradictions, missing_evidence และเหตุผล ห้ามสร้างคะแนน confidence เพราะ backend "
                "จะคำนวณเอง ห้ามสร้าง candidate หรือพิกัดใหม่ และอย่าให้"
                "ชื่อคล้ายเพียงอย่างเดียวมีคะแนนสูง พิจารณาความสัมพันธ์ที่มีหลักฐานระหว่าง legal entity, "
                "parent company, subsidiary, trade name, brand และชื่อสาขา โดยอย่าถือว่าชื่อต่างกันคือคนละ"
                "สถานที่ทันที และอย่าถือว่าเป็น entity เดียวกันโดยไม่มีหลักฐาน ข้อมูล registration_number/"
                "registered_address จาก DBD "
                "ใช้ยืนยันตัวบริษัทและช่วยสร้างคำค้นได้ แต่ห้ามถือว่าที่อยู่จดทะเบียนคือจุดรับส่งสินค้าโดย"
                "อัตโนมัติ Candidate ที่ไม่มีชื่อ POI แต่ที่อยู่ตรง registered_address สามารถยืนยันตัวบริษัท"
                "ได้ ให้ใส่ matched_fields=registered_address และอธิบายว่าเป็นสำนักงานจดทะเบียน ไม่ใช่"
                "operational site ส่วน area_context เป็นเพียงคะแนนพื้นที่แบบ soft hint ไม่ต้องตรงตัว "
                "ถ้ายังไม่มั่นใจให้คืน targeted query ใหม่ 3-5 รายการที่ไม่ซ้ำและอิง missing_evidence "
                "โดยแตกคำ ใช้ transliteration บริษัทแม่ alias โทรศัพท์ ที่อยู่เฉพาะ และพื้นที่รอบพิกัด "
                "แต่ละ query ไม่เกิน 120 ตัวอักษร เป็นคำค้นไทย/อังกฤษ/ตัวเลขล้วน ไม่มีคำอธิบายหรือคำถาม "
                "หาก raw ระบุ organization แต่ candidate ใช้ชื่ออื่น ให้ตรวจหลักฐานความสัมพันธ์ของชื่อค้า "
                "แบรนด์ บริษัทแม่ บริษัทลูก สาขา และผู้ดำเนินการก่อนตัดสิน หากไม่มีทั้ง name relationship "
                "และ strong identifier เช่นโทรศัพท์ เลขที่อยู่ หรือ registered address ให้ REJECT ห้ามชดเชย"
                "การไม่ตรง entity ด้วย landmark จังหวัด หรือรหัสไปรษณีย์ และห้ามเลือก geographic entity "
                "หรือ infrastructure แทนเป้าหมาย เว้นแต่ raw ระบุ entity ประเภทนั้นโดยตรง "
                "exhausted=true เฉพาะเมื่อกลยุทธ์ที่มีหลักฐานสมเหตุผลหมดแล้ว"
            ),
            json.dumps(
                {
                    "round": round_number,
                    "raw_input": raw_input,
                    "parsed": parsed.model_dump(mode="json"),
                    "attempted_queries": attempted_queries,
                    "candidate_locations": public_candidates,
                },
                ensure_ascii=False,
            ),
        )

    def _agent_payload(
        self, schema: dict[str, Any], schema_name: str, instructions: str, input_text: str
    ) -> dict[str, Any]:
        return {
            "model": self.model,
            "store": False,
            "reasoning": {"effort": "high"},
            "instructions": instructions,
            "input": input_text,
            "tools": [
                {
                    "type": "web_search",
                    "search_context_size": "high",
                    "user_location": {
                        "type": "approximate",
                        "country": "TH",
                        "timezone": "Asia/Bangkok",
                    },
                }
            ],
            "tool_choice": "auto",
            "include": ["web_search_call.action.sources"],
            "max_tool_calls": self.maximum_tool_calls,
            # Reasoning tokens count toward this budget. Quality-first agent runs
            # need enough room for high-effort reasoning plus structured JSON.
            "max_output_tokens": 12000,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                }
            },
        }

    @classmethod
    def _validated_dbd(
        cls,
        value: Any,
        evidence: list[ResearchEvidence],
        raw_input: str = "",
    ) -> DBDResearchStatus:
        if not isinstance(value, dict):
            return DBDResearchStatus()
        status = str(value.get("status") or "skipped")
        reason = str(value.get("reason") or "")[:500]
        candidate_queries = [
            re.sub(r"\s+", " ", str(item)).strip()[:180]
            for item in value.get("queries") or []
            if str(item).strip()
        ][:5]
        queries = cls._relevant_dbd_queries(candidate_queries, raw_input)
        # Web-search annotations are the trust boundary. Requiring the model to
        # copy the identical URL into source_urls caused valid DBD citations to
        # be discarded when query strings or fragments differed.
        dbd_evidence = [item for item in evidence if cls._is_official_dbd_url(item.url)]
        registration_number = str(value.get("registration_number") or "").strip()
        discovery_evidence = [
            item
            for item in evidence
            if cls._dataforthai_registration(item.url) == registration_number
        ]
        if status in {"searched", "discovered"} and not dbd_evidence and discovery_evidence:
            return DBDResearchStatus(
                status="discovered",
                reason="พบเลขทะเบียนจาก DataForThai เพื่อส่งต่อให้ backend ตรวจ DBD Open API",
                queries=queries,
                registration_number=registration_number,
                evidence=discovery_evidence,
            )
        if status in {"searched", "discovered"} and not dbd_evidence:
            return DBDResearchStatus(
                status="unavailable",
                reason=(
                    "ยังไม่มี URL DataForThai ที่ยืนยันเลขทะเบียน หรือหลักฐาน DBD ทางการ "
                    "จึงไม่ส่งข้อมูลที่ยังตรวจสอบไม่ได้เข้าระบบ"
                ),
                queries=queries,
            )
        if status != "searched":
            return DBDResearchStatus(
                status="not_found" if status == "not_found" else "skipped",
                reason=reason or DBDResearchStatus().reason,
                queries=queries,
            )
        return DBDResearchStatus(
            status="searched",
            reason=reason or "พบข้อมูลนิติบุคคลจากเว็บไซต์ทางการ DBD",
            queries=queries,
            company_name=str(value.get("company_name") or "").strip() or None,
            registration_number=registration_number or None,
            registered_address=(str(value.get("registered_address") or "").strip() or None),
            evidence=dbd_evidence,
        )

    @staticmethod
    def _relevant_dbd_queries(queries: list[str], raw_input: str) -> list[str]:
        if not raw_input:
            return queries
        generic = {
            "บริษัท",
            "จำกัด",
            "หจก",
            "company",
            "limited",
            "co",
            "ltd",
            "dbd",
            "datawarehouse",
        }
        raw_tokens = {
            token.casefold()
            for token in re.findall(r"[A-Za-z0-9ก-๙]+", raw_input)
            if token.casefold() not in generic
        }
        if not raw_tokens:
            return queries
        return [
            query
            for query in queries
            if raw_tokens
            & {
                token.casefold()
                for token in re.findall(r"[A-Za-z0-9ก-๙]+", query)
                if token.casefold() not in generic
            }
        ]

    @classmethod
    def _guard_unverified_acronym_expansion(
        cls,
        parsed_values: dict[str, Any],
        raw_input: str,
        trusted_company_name: bool | DBDResearchStatus,
    ) -> dict[str, Any]:
        """Keep a spelled acronym literal unless cited web evidence expands it."""
        is_trusted = (
            trusted_company_name.company_name is not None
            if isinstance(trusted_company_name, DBDResearchStatus)
            else trusted_company_name
        )
        if is_trusted:
            return parsed_values
        tokens = re.findall(r"[A-Za-z]+|[ก-๙]+", raw_input)
        thai_letters = {
            "เอ": "A",
            "บี": "B",
            "ซี": "C",
            "ดี": "D",
            "อี": "E",
            "เอฟ": "F",
            "จี": "G",
            "เอช": "H",
            "ไอ": "I",
            "เจ": "J",
            "เค": "K",
            "แอล": "L",
            "เอ็ม": "M",
            "เอ็น": "N",
            "โอ": "O",
            "พี": "P",
            "คิว": "Q",
            "อาร์": "R",
            "เอส": "S",
            "ที": "T",
            "ยู": "U",
            "วี": "V",
            "ดับเบิลยู": "W",
            "เอ็กซ์": "X",
            "วาย": "Y",
            "แซด": "Z",
        }
        sequences: list[tuple[str, str]] = []
        letters: list[str] = []
        spellings: list[str] = []
        for token in [*tokens, ""]:
            letter = thai_letters.get(token)
            if letter:
                letters.append(letter)
                spellings.append(token)
            else:
                if len(letters) >= 2:
                    sequences.append(("".join(letters), "".join(spellings)))
                letters, spellings = [], []
        if not sequences:
            return parsed_values
        acronym, thai_spelling = max(sequences, key=lambda item: len(item[0]))

        def compact(value: Any) -> str:
            return "".join(char.casefold() for char in str(value or "") if char.isalnum())

        company = compact(parsed_values.get("company_name"))
        if acronym.casefold() in company or thai_spelling in company:
            return parsed_values
        guarded = dict(parsed_values)
        guarded["company_name"] = acronym
        aliases = [
            str(alias)
            for alias in guarded.get("aliases") or []
            if acronym.casefold() in compact(alias) or thai_spelling in compact(alias)
        ]
        guarded["aliases"] = list(dict.fromkeys([acronym, *aliases]))
        return guarded

    @classmethod
    def _guard_unverified_web_identity(
        cls,
        parsed_values: dict[str, Any],
        fallback: ParsedLocation,
        raw_input: str,
        trusted_company_name: bool,
    ) -> dict[str, Any]:
        """Prevent uncited web-name transformations from reaching DBD."""
        if trusted_company_name:
            return parsed_values
        raw_key = cls._normalized_entity(raw_input)
        company_key = cls._normalized_entity(parsed_values.get("company_name"))
        if company_key and company_key in raw_key:
            return parsed_values
        guarded = dict(parsed_values)
        guarded["company_name"] = fallback.company_name
        guarded["aliases"] = [
            str(alias)
            for alias in guarded.get("aliases") or []
            if cls._normalized_entity(alias) in raw_key
        ]
        return guarded

    @classmethod
    def _company_name_evidence(
        cls,
        source_urls: Any,
        evidence: list[ResearchEvidence],
    ) -> list[ResearchEvidence]:
        """Accept company-name citations only from the pre-registry web phase."""
        requested = {
            cls._canonical_url(str(value))
            for value in (source_urls or [])
            if str(value).strip()
        }
        return [
            item
            for item in evidence
            if cls._canonical_url(item.url) in requested
            and not cls._is_registry_url(item.url)
        ]

    @staticmethod
    def _registration_from_evidence(evidence: list[ResearchEvidence]) -> str | None:
        for item in evidence:
            match = re.search(
                r"(?:company(?:-branch)?/|juristic_person/)(\d{13})(?:/|$)",
                item.url,
                re.IGNORECASE,
            )
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _canonical_url(value: str) -> str:
        parsed = urlparse(value)
        return f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}{parsed.path.rstrip('/')}"

    @staticmethod
    def _is_registry_url(value: str) -> bool:
        hostname = (urlparse(value).hostname or "").casefold().rstrip(".")
        return any(
            hostname == domain or hostname.endswith(f".{domain}")
            for domain in ("dbd.go.th", "rd.go.th", "dataforthai.com")
        )

    @staticmethod
    def _normalized_entity(value: Any) -> str:
        return "".join(
            character.casefold() for character in str(value or "") if character.isalnum()
        )

    @staticmethod
    def _is_official_dbd_url(value: str) -> bool:
        hostname = (urlparse(value).hostname or "").casefold().rstrip(".")
        return hostname == "dbd.go.th" or hostname.endswith(".dbd.go.th")

    @staticmethod
    def _dataforthai_registration(value: str) -> str | None:
        parsed = urlparse(value)
        hostname = (parsed.hostname or "").casefold().rstrip(".")
        if hostname not in {"dataforthai.com", "www.dataforthai.com"}:
            return None
        match = re.search(r"/company/(\d{13})(?:/|$)", parsed.path)
        return match.group(1) if match else None

    @classmethod
    def _extract_registration_number(cls, raw_input: str) -> str | None:
        match = cls._registration_pattern.search(raw_input)
        return match.group(0) if match else None

    @staticmethod
    def _parse_json_output(data: dict[str, Any]) -> dict[str, Any]:
        for item in data.get("output", []):
            if item.get("type") == "message":
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        value = json.loads(content.get("text", "{}"))
                        if isinstance(value, dict):
                            return value
        raise ValueError("OpenAI response did not contain structured output")

    def _unique_safe_queries(self, values: list[Any], *, allow_phone: bool) -> list[str]:
        output: list[str] = []
        for value in values:
            cleaned = re.sub(r"\s+", " ", str(value)).strip(" ,\"'`")[:120]
            supported = re.fullmatch(r"[A-Za-z0-9ก-๙\s.,/&()+\-]+", cleaned)
            if len(cleaned) < 2 or not supported or self._coordinate_pattern.search(cleaned):
                continue
            if not allow_phone and self._phone_pattern.search(cleaned):
                continue
            if cleaned.casefold() not in {item.casefold() for item in output}:
                output.append(cleaned)
            if len(output) >= self.maximum_queries:
                break
        return output

    @staticmethod
    def _public_context(parsed: ParsedLocation, candidates: list[LocationCandidate]) -> str | None:
        # Only public POI/business-style searches are eligible. A phone-only query,
        # customer reference, or residential address is intentionally excluded.
        if not OpenAIResearchService.is_eligible(parsed):
            return None
        parts = [
            f"ชื่อสถานที่: {parsed.place_name}",
            f"แขวง/ตำบล: {parsed.subdistrict}" if parsed.subdistrict else None,
            f"เขต/อำเภอ: {parsed.district}" if parsed.district else None,
            f"จังหวัด: {parsed.province}" if parsed.province else None,
        ]
        public_candidates = []
        for candidate in candidates[:3]:
            if candidate.name:
                public_candidates.append(
                    " | ".join(filter(None, [candidate.name, candidate.address]))[:300]
                )
        if public_candidates:
            parts.append("ผลจากผู้ให้บริการแผนที่: " + "; ".join(public_candidates))
        return "\n".join(part for part in parts if part)

    def _payload(self, public_context: str) -> dict[str, Any]:
        schema = {
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": self.maximum_queries,
                },
                "aliases": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 5,
                },
                "summary": {"type": "string"},
            },
            "required": ["queries", "aliases", "summary"],
            "additionalProperties": False,
        }
        return {
            "model": self.model,
            "store": False,
            "instructions": (
                "คุณเป็นผู้ช่วยค้นหาและระบุตัวตนของสถานที่สาธารณะในประเทศไทย "
                "วิเคราะห์ข้อมูลดิบที่ได้รับทั้งหมด แม้ข้อมูลอาจซ้ำ ผิดรูปแบบ สะกดผิด หรือไม่ครบถ้วน "
                "จากนั้นใช้เว็บค้นหาหลักฐานจากเว็บไซต์ทางการ รายชื่อธุรกิจ หรือแหล่งข้อมูลสาธารณะที่ตรวจสอบได้ "
                "เพื่อค้นหาชื่อสถานที่ ชื่อเรียกอื่น ชื่อสาขา และส่วนของที่อยู่ที่เกี่ยวข้อง "
                "ห้ามสร้าง คาดเดา หรือระบุค่าพิกัด latitude/longitude หรือค่าตำแหน่งเชิงตัวเลขใด ๆ "
                "ห้ามเติมข้อมูลที่ไม่มีหลักฐานรองรับ และห้ามใช้ข้อมูลส่วนบุคคลที่ไม่จำเป็น "
                "หากพบหลายสถานที่ที่เป็นไปได้ ให้เก็บความเป็นไปได้ไว้หลายรายการแทนการเลือกโดยไม่มีหลักฐานเพียงพอ "
                "เป้าหมายสุดท้ายคือสร้างคำค้นที่สั้น กระชับ และมีความเฉพาะเจาะจง "
                "สำหรับนำไปค้นต่อกับ Google Maps, HERE หรือ Longdo"
            ),
            "input": (
                "วิเคราะห์ Raw Data ต่อไปนี้โดยใช้ข้อมูลทั้งหมดร่วมกัน "
                "ค้นหาชื่อเรียกอื่น ชื่อสาขา จังหวัด เขต/อำเภอ แขวง/ตำบล ถนน หรือข้อมูลสถานที่สาธารณะ "
                "ที่ช่วยแยกสถานที่ออกจากสถานที่ชื่อใกล้เคียง "
                "จากนั้นสร้างคำค้นหลายรูปแบบ เรียงจากคำค้นที่มีโอกาสระบุตำแหน่งได้แม่นยำที่สุดก่อน "
                "ห้ามใส่พิกัด และห้ามเดาข้อมูลที่ไม่มีหลักฐาน\n\n" + public_context
            ),
            "tools": [
                {
                    "type": "web_search",
                    "search_context_size": "low",
                    "user_location": {
                        "type": "approximate",
                        "country": "TH",
                        "timezone": "Asia/Bangkok",
                    },
                }
            ],
            "tool_choice": "auto",
            "include": ["web_search_call.action.sources"],
            "max_tool_calls": self.maximum_tool_calls,
            "max_output_tokens": 4000,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "location_research_hints",
                    "strict": True,
                    "schema": schema,
                }
            },
        }

    @staticmethod
    def _parse_hints(data: dict[str, Any]) -> ResearchHints:
        for item in data.get("output", []):
            if item.get("type") != "message":
                continue
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    try:
                        return ResearchHints.model_validate_json(content.get("text", ""))
                    except ValidationError as exc:
                        raise ValueError("OpenAI returned invalid research hints") from exc
        raise ValueError("OpenAI response did not contain research hints")

    @classmethod
    def _extract_evidence(cls, data: dict[str, Any], *, limit: int = 8) -> list[ResearchEvidence]:
        evidence: list[ResearchEvidence] = []
        seen: set[str] = set()

        def add(url: Any, title: Any = None) -> None:
            if not isinstance(url, str) or url in seen or not cls._safe_url(url):
                return
            seen.add(url)
            evidence.append(ResearchEvidence(title=str(title or url)[:200], url=url))

        for item in data.get("output", []):
            action = item.get("action") or {}
            for source in action.get("sources") or []:
                add(source.get("url"), source.get("title"))
            for content in item.get("content") or []:
                for annotation in content.get("annotations") or []:
                    add(annotation.get("url"), annotation.get("title"))
        return evidence[:limit]

    @classmethod
    def _safe_query(cls, value: str) -> str | None:
        cleaned = re.sub(r"\s+", " ", str(value)).strip(" ,")[:180]
        if (
            len(cleaned) < 2
            or cls._phone_pattern.search(cleaned)
            or cls._coordinate_pattern.search(cleaned)
        ):
            return None
        return cleaned

    @staticmethod
    def _safe_url(value: str) -> bool:
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

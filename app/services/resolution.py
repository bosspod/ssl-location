import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from app.config import Settings
from app.database import MongoDatabase
from app.providers.base import MapProvider
from app.repositories import LocationRepository, location_id
from app.schemas import (
    ConfidenceStatus,
    ConfirmRequest,
    ConfirmResponse,
    LocationCandidate,
    MapPreview,
    ParsedLocation,
    ProviderStatus,
    ResearchEvidence,
    ResearchStatus,
    ResolveRequest,
    ResolveResponse,
)
from app.services.cache import Cache, MemoryTTLCache
from app.services.candidates import CandidateAggregator, CandidateDeduplicator, CandidateFilter
from app.services.enrichment import CandidateEnrichmentService
from app.services.parser import LocationParserService
from app.services.query_generator import SearchQueryGenerator
from app.services.registry import BusinessRegistryService
from app.services.research import (
    AgentResolution,
    AgentSearchPlan,
    CandidateEvaluation,
    LocationResearcher,
)
from app.services.scoring import ConfidenceService, LocationScoringService
from app.utils import admin_area_similarity, mask_phones, place_name_similarity, string_similarity

logger = logging.getLogger("location_resolution")
ProgressReporter = Callable[[dict[str, Any]], Awaitable[None]]


async def no_progress(_: dict[str, Any]) -> None:
    return None


class LocationResolutionService:
    def __init__(
        self,
        settings: Settings,
        providers: list[MapProvider],
        cache: Cache[list[LocationCandidate]],
        researcher: LocationResearcher | None = None,
        registry: BusinessRegistryService | None = None,
    ) -> None:
        self.settings = settings
        self.providers = providers
        self.provider_semaphore = asyncio.Semaphore(settings.provider_concurrency)
        self.circuit_lock = asyncio.Lock()
        self.circuit_failures: dict[str, int] = {}
        self.circuit_open_until: dict[str, float] = {}
        self.cache = cache
        self._searches: dict[str, asyncio.Task[tuple[list[LocationCandidate], ProviderStatus]]] = {}
        self._plans: MemoryTTLCache[AgentSearchPlan] = MemoryTTLCache(settings.cache_max_entries)
        self._judgments: MemoryTTLCache[AgentResolution] = MemoryTTLCache(
            settings.cache_max_entries
        )
        self.researcher = researcher
        self.registry = registry
        self.parser = LocationParserService()
        self.query_generator = SearchQueryGenerator(settings.max_search_queries)
        self.aggregator = CandidateAggregator()
        self.deduplicator = CandidateDeduplicator()
        self.enricher = CandidateEnrichmentService(self.parser)
        self.filter = CandidateFilter()
        self.scorer = LocationScoringService()
        self.confidence = ConfidenceService(settings.top_candidate_min_margin)

    async def resolve(
        self,
        request: ResolveRequest,
        database: MongoDatabase,
        progress: ProgressReporter = no_progress,
    ) -> ResolveResponse:
        started = time.perf_counter()
        request_id = str(uuid4())
        selected_model = request.ai_model or self.settings.ai_research_model
        researcher = self.researcher.with_model(selected_model) if self.researcher else None
        await progress(
            {
                "stage": "received",
                "title": "รับข้อมูลดิบแล้ว",
                "detail": request.input,
            }
        )
        parsed = self.parser.parse(request.input)
        original_parsed = parsed.model_copy(deep=True)
        research = ResearchStatus(status="disabled" if not researcher else "planning")
        agent_plan = None
        if researcher:
            await progress(
                {
                    "stage": "ai_analysis",
                    "title": "AI กำลังทำ Web Search และวาง Search Strategy",
                    "detail": (
                        f"ใช้ {selected_model} แปลงชื่อบริษัทและสร้างคำค้นจากหลักฐานเว็บ "
                        "ก่อนตรวจ DBD"
                    ),
                }
            )
            try:
                plan_key = json.dumps(
                    [
                        selected_model,
                        parsed.normalized_input,
                        request.context.model_dump() if request.context else None,
                    ],
                    sort_keys=True,
                    ensure_ascii=False,
                )
                agent_plan = await self._plans.get(plan_key)
                if agent_plan is None:
                    agent_plan = await researcher.plan(request.input, parsed, request.context)
                    if agent_plan and (
                        agent_plan.parsed.company_name
                        or agent_plan.parsed.place_name
                        or agent_plan.company_name_evidence
                    ):
                        await self._plans.set(
                            plan_key,
                            agent_plan.model_copy(deep=True),
                            self.settings.cache_ttl_seconds,
                        )
                if agent_plan:
                    agent_plan = agent_plan.model_copy(deep=True)
                    parsed = agent_plan.parsed
                    # Explicit raw identifiers survive an incomplete AI extraction.
                    for field in (
                        "phone",
                        "house_number",
                        "village",
                        "building",
                        "soi",
                        "road",
                        "subdistrict",
                        "district",
                        "province",
                        "postal_code",
                    ):
                        if not getattr(parsed, field) and getattr(original_parsed, field):
                            setattr(parsed, field, getattr(original_parsed, field))
                            if field in original_parsed.entity_confidence:
                                parsed.entity_confidence[field] = original_parsed.entity_confidence[
                                    field
                                ]
                    research = ResearchStatus(
                        status="success",
                        query_hints=agent_plan.queries,
                        evidence=agent_plan.evidence,
                        summary=agent_plan.summary,
                    )
            except Exception as exc:
                logger.warning("location_ai_plan_failed error=%s", type(exc).__name__)
                research = ResearchStatus(status="error", error=type(exc).__name__)
        if agent_plan:
            await progress(
                {
                    "stage": "web_strategy",
                    "title": "Web Search แปลงชื่อบริษัทและสร้าง Search Strategy แล้ว",
                    "detail": {
                        "company_name": parsed.company_name,
                        "aliases": parsed.aliases,
                        "queries": agent_plan.queries,
                        "company_name_sources": [
                            item.model_dump(mode="json")
                            for item in agent_plan.company_name_evidence
                        ],
                    },
                }
            )
        registry_queries: list[str] = []
        # In AI mode the registry phase must never run with the unresearched raw
        # name. Deployments without an AI researcher retain deterministic fallback.
        if self.registry and (not researcher or agent_plan):
            await progress(
                {
                    "stage": "registry_search",
                    "title": "กำลังตรวจข้อมูลบริษัทกับ DBD",
                    "detail": {
                        "company_name": parsed.company_name,
                        "aliases": parsed.aliases,
                        "sequence": "DBD name search (เมื่อมีสิทธิ์) หรือ lookup ด้วยเลขทะเบียน 13 หลัก",
                    },
                }
            )
            try:
                registry_result = await self.registry.research(parsed)
                parsed = registry_result.parsed
                registry_queries = registry_result.map_queries
                if registry_result.dbd.status != "skipped":
                    research.dbd = registry_result.dbd
                research.evidence = self._merge_evidence(
                    research.evidence, registry_result.evidence
                )
                await progress(
                    {
                        "stage": "registry_result",
                        "title": "ตรวจทะเบียน DBD แล้ว",
                        "detail": {
                            "dbd": research.dbd.model_dump(mode="json"),
                        },
                    }
                )
            except Exception as exc:
                logger.warning("business_registry_failed error=%s", type(exc).__name__)
                await progress(
                    {
                        "stage": "registry_result",
                        "title": "Registry API ใช้งานไม่ได้ชั่วคราว",
                        "detail": {"error": type(exc).__name__},
                    }
                )
        await progress(
            {
                "stage": "extracted",
                "title": "แยกและ Normalize ข้อมูลแล้ว",
                "detail": parsed.model_dump(
                    mode="json", exclude={"raw_input", "entity_confidence"}
                ),
            }
        )
        internal = await LocationRepository(database).search(parsed)
        internal = [self.scorer.score(parsed, candidate, request.context) for candidate in internal]
        internal.sort(key=lambda item: item.score, reverse=True)
        exact_internal = internal[0] if internal else None
        unambiguous_internal = (
            exact_internal is not None
            and exact_internal.score_breakdown.get("exactMasterMatch", 0) > 0
            and exact_internal.verification_status == "USER_CONFIRMED"
            and (
                len(internal) == 1
                or exact_internal.score - internal[1].score
                >= self.settings.top_candidate_min_margin
            )
        )
        if exact_internal and unambiguous_internal and agent_plan is None:
            self._populate_result_contract([exact_internal], False)
            exact_internal.confirmation_token = self._sign(exact_internal, request.input)
            processing_time_ms = round((time.perf_counter() - started) * 1000, 2)
            response = ResolveResponse(
                request_id=request_id,
                query=request.input,
                parsed=parsed,
                status=self.confidence.classify([exact_internal]),
                best_match=exact_internal,
                alternatives=[],
                map_preview=self._map_preview(exact_internal),
                providers=[
                    ProviderStatus(provider="location_master", status="success", latency_ms=0)
                ],
                research=research,
                processing_time_ms=processing_time_ms,
            )
            await progress(
                {
                    "stage": "complete",
                    "title": "พบข้อมูลยืนยันใน Location Master",
                    "detail": {
                        "status": response.status.value,
                        "confidence": 1.0,
                        "accepted": True,
                        "top_candidate": exact_internal.name,
                        "processing_time_ms": processing_time_ms,
                    },
                }
            )
            self._log(response, request, started, len(internal), len(internal))
            return response

        query_pool = self._precision_queries(
            parsed,
            [
                original_parsed.normalized_input,
                *registry_queries,
                *(agent_plan.queries if agent_plan else []),
                *self.query_generator.generate(parsed),
                *self.query_generator.generate(original_parsed),
            ],
            self.settings.max_search_queries,
        )
        queries = query_pool[: self.settings.initial_search_queries]
        await progress(
            {
                "stage": "strategy",
                "title": f"สร้าง Search Strategy {len(queries)} แบบ",
                "detail": queries,
            }
        )
        external, statuses = await self._search_external(queries)
        await progress(
            {
                "stage": "map_search",
                "title": "ค้นหาจาก Map Providers แล้ว",
                "detail": {
                    "candidate_count": len(external),
                    "providers": [status.model_dump(mode="json") for status in statuses],
                    "candidate_locations": [
                        candidate.model_dump(
                            mode="json",
                            include={
                                "name",
                                "address",
                                "phone",
                                "latitude",
                                "longitude",
                                "sources",
                                "source_query",
                            },
                        )
                        for candidate in external[: self.settings.ai_candidate_limit]
                    ],
                },
            }
        )
        ranked, deduplicated = self._rank(parsed, request, internal, external)
        ranked = await self._enrich_ranked(parsed, request, ranked)
        await progress(
            {
                "stage": "candidate_enrichment",
                "title": "ตรวจรายละเอียด Candidate อันดับต้นแล้ว",
                "detail": {
                    "candidate_count": min(len(ranked), self.settings.google_place_details_limit),
                    "source": "Google Place Details (เมื่อมี Google Place ID)",
                },
            }
        )
        initial_status = self.confidence.classify(ranked)
        used_agent_resolution = False
        evaluations: dict[str, CandidateEvaluation] = {}
        if researcher and agent_plan:
            attempted_queries = list(queries)
            evidence = list(research.evidence)
            for round_number in range(1, self.settings.ai_research_max_rounds + 1):
                ai_candidates = self._select_ai_candidates(ranked)
                await progress(
                    {
                        "stage": "entity_resolution",
                        "title": f"AI Entity Resolution รอบ {round_number}",
                        "detail": {
                            "candidate_count_total": len(ranked),
                            "candidate_count_sent_to_ai": len(ai_candidates),
                            "truncated": len(ai_candidates) < len(ranked),
                            "fields": [
                                "ชื่อ/บริษัท",
                                "เบอร์โทร",
                                "ที่อยู่",
                                "บริเวณโดยประมาณ",
                                "พิกัด",
                            ],
                        },
                    }
                )
                try:
                    judgment_key = hashlib.sha256(
                        json.dumps(
                            [
                                selected_model,
                                parsed.model_dump(),
                                attempted_queries,
                                round_number,
                                [
                                    item.model_dump(exclude={"raw", "confirmation_token"})
                                    for item in ai_candidates
                                ],
                            ],
                            sort_keys=True,
                            ensure_ascii=False,
                            default=str,
                        ).encode()
                    ).hexdigest()
                    decision = await self._judgments.get(judgment_key)
                    if decision is None:
                        decision = await researcher.resolve_candidates(
                            request.input,
                            parsed,
                            ai_candidates,
                            attempted_queries,
                            round_number,
                        )
                        if decision and decision.evaluations:
                            await self._judgments.set(
                                judgment_key,
                                decision.model_copy(deep=True),
                                self.settings.cache_ttl_seconds,
                            )
                except Exception as exc:
                    logger.warning("location_ai_resolution_failed error=%s", type(exc).__name__)
                    research = research.model_copy(
                        update={
                            "status": "error",
                            "error": type(exc).__name__,
                            "rounds": round_number,
                        }
                    )
                    break
                if decision is None:
                    break
                used_agent_resolution = used_agent_resolution or bool(decision.evaluations)
                evidence = self._merge_evidence(evidence, decision.evidence)
                evaluations.update({item.candidate_id: item for item in decision.evaluations})
                ranked = self._apply_ai_resolution(ranked, list(evaluations.values()))
                await progress(
                    {
                        "stage": "resolution_result",
                        "title": f"ผลเปรียบเทียบ Candidate รอบ {round_number}",
                        "detail": {
                            "top_candidates": [
                                {
                                    "place_name": candidate.name,
                                    "normalized_address": candidate.address,
                                    "latitude": candidate.latitude,
                                    "longitude": candidate.longitude,
                                    "confidence_score": candidate.confidence_score,
                                    "matched_fields": candidate.matched_fields,
                                    "reason": candidate.reason,
                                    "sources": candidate.sources,
                                }
                                for candidate in ranked[:5]
                            ],
                            "additional_queries": decision.additional_queries,
                            "exhausted": decision.exhausted,
                        },
                    }
                )
                research = research.model_copy(
                    update={
                        "status": "success",
                        "rounds": round_number,
                        "exhausted": decision.exhausted,
                        "summary": decision.summary or research.summary,
                        "evidence": evidence,
                    }
                )
                new_queries = self._untried_queries(
                    parsed,
                    [*decision.additional_queries, *query_pool],
                    attempted_queries,
                    self.settings.additional_search_queries,
                )
                if (
                    self._search_satisfied(ranked)
                    or not new_queries
                    or round_number >= self.settings.ai_research_max_rounds
                ):
                    research = research.model_copy(
                        update={
                            "exhausted": not new_queries
                            or round_number >= self.settings.ai_research_max_rounds
                        }
                    )
                    break
                remaining = self.settings.max_search_queries - len(attempted_queries)
                new_queries = new_queries[: max(0, remaining)]
                if not new_queries:
                    research = research.model_copy(update={"exhausted": True})
                    break
                attempted_queries.extend(new_queries)
                research.query_hints = list(attempted_queries)
                await progress(
                    {
                        "stage": "retry_search",
                        "title": f"ยังไม่มั่นใจ—ค้นหาเพิ่มรอบ {round_number + 1}",
                        "detail": new_queries,
                    }
                )
                found, more_statuses = await self._search_external(new_queries)
                external.extend(found)
                statuses = self._combine_provider_statuses(statuses, more_statuses)
                ranked, deduplicated = self._rank(parsed, request, internal, external)
                ranked = await self._enrich_ranked(parsed, request, ranked)
                ranked = self._apply_ai_resolution(ranked, list(evaluations.values()))
            queries = attempted_queries

        uncertain = initial_status.value in {
            "MEDIUM",
            "LOW",
            "CONFLICT",
            "NO_RESULT",
        }
        if researcher and not agent_plan and uncertain and not researcher.is_eligible(parsed):
            research = ResearchStatus(status="not_eligible")
        elif researcher and not agent_plan and uncertain and research.status != "error":
            try:
                research = await researcher.research(parsed, queries, ranked[:5])
                if research.query_hints:
                    research.query_hints = self._untried_queries(
                        parsed,
                        research.query_hints,
                        queries,
                        self.settings.max_search_queries - len(queries),
                    )
                    researched_candidates, researched_statuses = await self._search_external(
                        research.query_hints
                    )
                    external.extend(researched_candidates)
                    queries.extend(research.query_hints)
                    statuses = self._combine_provider_statuses(statuses, researched_statuses)
                    ranked, deduplicated = self._rank(parsed, request, internal, external)
                    ranked = await self._enrich_ranked(parsed, request, ranked)
            except Exception as exc:
                logger.warning("location_research_failed error=%s", type(exc).__name__)
                research = ResearchStatus(status="error", error=type(exc).__name__)
        # Always spend the remaining useful deterministic queries when AI is absent,
        # fails, or stops suggesting queries before we have a strong result.
        while not self._search_satisfied(ranked):
            pending = self._untried_queries(
                parsed,
                query_pool,
                queries,
                min(
                    self.settings.additional_search_queries,
                    self.settings.max_search_queries - len(queries),
                ),
            )
            if not pending:
                break
            queries.extend(pending)
            await progress(
                {
                    "stage": "retry_search",
                    "title": "ค้นเพิ่มเติมจากชื่อและที่อยู่เดิม",
                    "detail": pending,
                }
            )
            found, more_statuses = await self._search_external(pending)
            external.extend(found)
            statuses = self._combine_provider_statuses(statuses, more_statuses)
            ranked, deduplicated = self._rank(parsed, request, internal, external)
            ranked = await self._enrich_ranked(parsed, request, ranked)
            ranked = self._apply_ai_resolution(ranked, list(evaluations.values()))
        research.query_hints = list(queries)
        self._attach_evidence_verified_phone(parsed, ranked, research.evidence)
        self._populate_result_contract(ranked, used_agent_resolution)
        best_match, alternatives = self._select_top_result(
            ranked,
            require_ai_verdict=used_agent_resolution,
        )
        map_preview_candidate = best_match or self._select_address_preview(parsed, deduplicated)
        selected_ranked = ([best_match] if best_match else []) + alternatives
        before = len(internal) + len(external)
        for candidate in ([best_match] if best_match else []) + alternatives:
            candidate.confirmation_token = self._sign(candidate, request.input)
        processing_time_ms = round((time.perf_counter() - started) * 1000, 2)
        response = ResolveResponse(
            request_id=request_id,
            query=request.input,
            parsed=parsed,
            status=(
                self.confidence.classify(selected_ranked)
                if best_match
                else ConfidenceStatus.NO_RESULT
            ),
            best_match=best_match,
            alternatives=alternatives,
            map_preview=self._map_preview(map_preview_candidate),
            providers=statuses,
            research=research,
            processing_time_ms=processing_time_ms,
        )
        await progress(
            {
                "stage": "complete",
                "title": (
                    "เลือก Candidate ที่มั่นใจที่สุดแล้ว"
                    if response.best_match
                    else (
                        "ปักหมุดจากผลแผนที่ให้ตรวจสอบแล้ว"
                        if response.map_preview
                        else "ยังไม่พบ Candidate ที่เกี่ยวข้อง"
                    )
                ),
                "detail": {
                    "status": response.status.value,
                    "confidence": best_match.confidence_score if best_match else 0,
                    "accepted": response.best_match is not None,
                    "top_candidate": best_match.name if best_match else None,
                    "map_preview": response.map_preview.name if response.map_preview else None,
                    "processing_time_ms": processing_time_ms,
                    "rounds": response.research.rounds,
                    "exhausted": response.research.exhausted,
                },
            }
        )
        self._log(response, request, started, before, len(deduplicated), queries)
        return response

    @staticmethod
    def _select_top_result(
        ranked: list[LocationCandidate],
        *,
        require_ai_verdict: bool = False,
    ) -> tuple[LocationCandidate | None, list[LocationCandidate]]:
        """Return the strongest eligible candidate; confidence remains informational."""
        eligible = [
            candidate
            for candidate in ranked
            if not require_ai_verdict
            or candidate.ai_verdict in {"MATCH", "POSSIBLE"}
        ]
        if not eligible:
            return None, []
        return eligible[0], eligible[1:6]

    @staticmethod
    def _map_preview(candidate: LocationCandidate | None) -> MapPreview | None:
        if candidate is None:
            return None
        return MapPreview(
            latitude=candidate.latitude,
            longitude=candidate.longitude,
            address=candidate.normalized_address or candidate.address,
            name=candidate.name,
            postal_code=candidate.postal_code,
            provider=candidate.provider,
        )

    @staticmethod
    def _select_address_preview(
        parsed: ParsedLocation,
        candidates: list[LocationCandidate],
    ) -> LocationCandidate | None:
        """Pick a useful inspection coordinate without treating it as an entity match."""
        identities = [parsed.company_name, parsed.place_name, *parsed.aliases]
        identity_keys = [
            "".join(char.casefold() for char in identity if char.isalnum())
            for identity in identities
            if identity and any(char.isalpha() for char in identity)
        ]
        named: list[tuple[float, LocationCandidate]] = []
        for candidate in candidates:
            if not candidate.name:
                continue
            name_key = "".join(char.casefold() for char in candidate.name if char.isalnum())
            contains_identity = any(
                len(name_key) >= 5
                and (
                    name_key in identity_key
                    or (len(identity_key) >= 5 and identity_key in name_key)
                )
                for identity_key in identity_keys
            )
            if not contains_identity:
                continue
            geography_score = 0.0
            geographic_matches = 0
            contradicted = False
            for field in ("subdistrict", "district", "province"):
                expected = getattr(parsed, field)
                actual = getattr(candidate, field)
                if not expected or not actual:
                    continue
                similarity = admin_area_similarity(expected, actual)
                if similarity < 0.5:
                    contradicted = True
                    break
                geography_score += similarity
                if similarity >= 0.8:
                    geographic_matches += 1
            if not contradicted and (not parsed.province or geographic_matches >= 1):
                named.append((len(name_key) + geography_score, candidate))
        if named:
            return max(named, key=lambda item: item[0])[1]

        if not parsed.house_number:
            return None

        scored: list[tuple[float, LocationCandidate]] = []
        for candidate in candidates:
            if not candidate.house_number:
                continue
            expected_house = parsed.house_number.replace(" ", "")
            actual_house = candidate.house_number.replace(" ", "")
            if expected_house != actual_house:
                continue
            if parsed.postal_code and candidate.postal_code:
                if parsed.postal_code != candidate.postal_code:
                    continue

            score = 6.0
            geographic_matches = 0
            contradicted = False
            for field in ("subdistrict", "district", "province"):
                expected = getattr(parsed, field)
                actual = getattr(candidate, field)
                if not expected or not actual:
                    continue
                similarity = admin_area_similarity(expected, actual)
                if similarity < 0.5:
                    contradicted = True
                    break
                if similarity >= 0.8:
                    geographic_matches += 1
                    score += 2.0
            if contradicted:
                continue
            if parsed.postal_code and candidate.postal_code:
                score += 3.0
            address_target = " ".join(
                filter(
                    None,
                    [
                        parsed.house_number,
                        parsed.moo,
                        parsed.village,
                        parsed.building,
                        parsed.soi,
                        parsed.road,
                        parsed.subdistrict,
                        parsed.district,
                        parsed.province,
                        parsed.postal_code,
                    ],
                )
            )
            score += 3.0 * string_similarity(address_target, candidate.address)
            if geographic_matches >= 2 or (
                geographic_matches >= 1
                and parsed.postal_code
                and candidate.postal_code == parsed.postal_code
            ):
                scored.append((score, candidate))

        if scored:
            return max(scored, key=lambda item: item[0])[1]

        if parsed.company_name or parsed.place_name:
            return None

        # Google Places can return a POI for a full address even when its listing
        # omits the house number. Surface the first raw-query hit for inspection,
        # never as a confirmed destination.
        if not (parsed.subdistrict or parsed.district) or not parsed.province:
            return None
        for candidate in candidates:
            if (
                candidate.provider != "google"
                or not candidate.name
                or candidate.source_query != parsed.normalized_input
            ):
                continue
            if (
                candidate.house_number
                and candidate.house_number.replace(" ", "")
                != parsed.house_number.replace(" ", "")
            ):
                continue
            if (
                candidate.province
                and admin_area_similarity(parsed.province, candidate.province) < 0.8
            ):
                continue
            if parsed.district and candidate.district:
                if admin_area_similarity(parsed.district, candidate.district) < 0.8:
                    continue
            return candidate
        return None

    def _candidate_matches_company(
        self,
        parsed: ParsedLocation,
        candidate: LocationCandidate,
        acceptance_threshold: float | None = None,
    ) -> bool:
        def compact(value: str | None) -> str:
            return "".join(char.casefold() for char in (value or "") if char.isalnum())

        if self._candidate_matches_registered_address(parsed, candidate):
            candidate.location_role = "registered_office"
            candidate.matched_fields = list(
                dict.fromkeys([*candidate.matched_fields, "registered_address"])
            )
        haystack = compact(" ".join(filter(None, [candidate.name, candidate.address])))
        for identity in [parsed.company_name, *parsed.aliases]:
            key = compact(identity)
            if len(key) >= 3 and key in haystack:
                return True
            if (
                identity
                and candidate.name
                and place_name_similarity(identity, candidate.name) >= 0.72
            ):
                return True
        if candidate.location_role == "registered_office":
            return True
        # A map POI commonly uses the consumer brand instead of its legal parent
        # company. Accept that representation only with independently matching
        # identifiers; area or name similarity alone is never enough.
        return self._candidate_has_strong_identity_anchors(parsed, candidate, acceptance_threshold)

    @staticmethod
    def _has_operational_site_evidence(candidate: LocationCandidate) -> bool:
        normalized = {
            "".join(character for character in field.casefold() if character.isalnum())
            for field in candidate.matched_fields
        }
        return bool(
            normalized
            & {
                "branch",
                "branchname",
                "site",
                "sitename",
                "sitelabel",
                "branchsite",
                "operationalsite",
            }
        )

    @staticmethod
    def _requires_operational_site_match(parsed: ParsedLocation) -> bool:
        label = parsed.branch_name or parsed.site_name
        if not label:
            return False

        def compact(value: str | None) -> str:
            return "".join(
                character.casefold() for character in (value or "") if character.isalnum()
            )

        label_key = compact(label)
        if len(label_key) < 3:
            return False
        identities = [parsed.company_name, parsed.place_name, *parsed.aliases]
        return not any(
            identity
            and (label_key == compact(identity) or place_name_similarity(label, identity) >= 0.9)
            for identity in identities
        )

    def _candidate_matches_registered_address(
        self, parsed: ParsedLocation, candidate: LocationCandidate
    ) -> bool:
        if not parsed.registered_address or not candidate.address:
            return False
        reference = self.parser.parse(parsed.registered_address)
        exact_matches = 0
        for field in ("subdistrict", "district", "province"):
            expected, actual = getattr(reference, field), getattr(candidate, field)
            if expected and actual and admin_area_similarity(expected, actual) >= 0.8:
                exact_matches += 1
        if (
            reference.postal_code
            and candidate.postal_code
            and reference.postal_code == candidate.postal_code
        ):
            exact_matches += 1
        return (
            exact_matches >= 2
            and string_similarity(parsed.registered_address, candidate.address) >= 0.45
        )

    def _candidate_has_strong_identity_anchors(
        self,
        parsed: ParsedLocation,
        candidate: LocationCandidate,
        acceptance_threshold: float | None = None,
    ) -> bool:
        threshold = (
            acceptance_threshold
            if acceptance_threshold is not None
            else self.settings.ai_accept_confidence
        )
        if candidate.confidence_score < threshold:
            return False
        matched = {field.casefold().replace("_", "") for field in candidate.matched_fields}
        if not matched & {"name", "placename", "alias", "aliases", "brandalias"}:
            return False
        parsed_phone = "".join(char for char in (parsed.phone or "") if char.isdigit())
        candidate_phone = "".join(char for char in (candidate.phone or "") if char.isdigit())
        phone_matches = bool(parsed_phone and parsed_phone == candidate_phone)
        house_matches = bool(
            parsed.house_number
            and candidate.house_number
            and parsed.house_number.replace(" ", "") == candidate.house_number.replace(" ", "")
        )
        geography_matches = len(matched & {"subdistrict", "district", "province", "postalcode"})
        return phone_matches or (house_matches and geography_matches >= 2)

    @staticmethod
    def _unique_queries(queries: list[str]) -> list[str]:
        output: list[str] = []
        seen: set[str] = set()
        for query in queries:
            cleaned = " ".join(query.split()).strip(" ,")
            if cleaned and cleaned.casefold() not in seen:
                seen.add(cleaned.casefold())
                output.append(cleaned)
        return output

    def _select_ai_candidates(self, ranked: list[LocationCandidate]) -> list[LocationCandidate]:
        """Keep the strongest candidates while preventing one query/provider flood."""
        limit = self.settings.ai_candidate_limit
        selected = list(ranked[: min(5, limit)])
        selected_ids = {candidate.candidate_id for candidate in selected}
        seen_queries = {self._query_key(candidate.source_query) for candidate in selected}
        seen_sources = {source for candidate in selected for source in candidate.sources}
        for candidate in ranked[len(selected) :]:
            query_key = self._query_key(candidate.source_query)
            introduces_evidence = bool(
                query_key not in seen_queries
                or any(source not in seen_sources for source in candidate.sources)
            )
            if introduces_evidence and candidate.candidate_id not in selected_ids:
                selected.append(candidate)
                selected_ids.add(candidate.candidate_id)
                seen_queries.add(query_key)
                seen_sources.update(candidate.sources)
            if len(selected) >= limit:
                return selected
        for candidate in ranked:
            if candidate.candidate_id not in selected_ids:
                selected.append(candidate)
            if len(selected) >= limit:
                break
        return selected

    def _has_sufficient_rule_evidence(
        self,
        ranked: list[LocationCandidate],
        acceptance_threshold: float | None = None,
    ) -> bool:
        threshold = (
            acceptance_threshold
            if acceptance_threshold is not None
            else self.settings.ai_accept_confidence
        )
        top = ranked[0]
        if top.score_breakdown.get("exactMasterMatch", 0) > 0:
            return True
        if top.score < threshold * 100:
            return False
        if len(ranked) > 1 and top.score - ranked[1].score < self.settings.top_candidate_min_margin:
            return False
        positive_identity = {
            key
            for key, value in top.score_breakdown.items()
            if value > 0
            and key
            in {
                "phone",
                "placeName",
                "companyName",
                "alias",
                "address",
                "houseNumber",
                "villageBuilding",
                "soi",
                "road",
                "subdistrict",
                "district",
                "province",
                "postalCode",
                "branchSite",
                "masterMatch",
            }
        }
        return "phone" in positive_identity or len(positive_identity) >= 2

    def _precision_queries(
        self, parsed: ParsedLocation, queries: list[str], limit: int
    ) -> list[str]:
        if limit <= 0:
            return []
        generic_values = {
            self._query_key(value)
            for value in (
                parsed.area_context,
                parsed.province,
                parsed.district,
                parsed.subdistrict,
                parsed.landmark,
            )
            if value
        }
        company_key = self._query_key(parsed.company_name)
        output: list[str] = []
        for query in self._unique_queries(queries):
            key = self._query_key(query)
            digits = "".join(char for char in query if char.isdigit())
            exact_identifier = len(digits) in {9, 10, 13}
            acronym_only = bool(
                company_key and key == company_key and len(key) <= 5 and key.isascii()
            )
            if not key or key in generic_values or acronym_only:
                continue
            if (
                not exact_identifier and len(query.split()) < 2 and len(key) <= 5
                and key != self._query_key(parsed.place_name)
            ):
                continue
            output.append(query)
            if len(output) >= limit:
                break
        return output

    def _untried_queries(
        self, parsed: ParsedLocation, proposed: list[str], attempted: list[str], limit: int
    ) -> list[str]:
        known = {self._query_key(query) for query in attempted}
        return self._precision_queries(
            parsed, [query for query in proposed if self._query_key(query) not in known], limit
        )

    @staticmethod
    def _search_satisfied(ranked: list[LocationCandidate]) -> bool:
        """Only controls further searching; never gates displaying the best result."""
        if not ranked:
            return False
        top = ranked[0]
        return bool(
            top.score >= 85
            and top.ai_verdict != "REJECT"
            and not top.contradictions
            and (len(ranked) == 1 or top.score - ranked[1].score >= 10)
        )

    @staticmethod
    def _query_key(value: str | None) -> str:
        return "".join(char.casefold() for char in (value or "") if char.isalnum())

    @staticmethod
    def _merge_evidence(
        first: list[ResearchEvidence], second: list[ResearchEvidence]
    ) -> list[ResearchEvidence]:
        output: list[ResearchEvidence] = []
        seen: set[str] = set()
        for item in [*first, *second]:
            url = str(getattr(item, "url", ""))
            if url and url not in seen:
                seen.add(url)
                output.append(item)
        return output

    @staticmethod
    def _apply_ai_resolution(
        ranked: list[LocationCandidate], evaluations: list[CandidateEvaluation]
    ) -> list[LocationCandidate]:
        by_id = {evaluation.candidate_id: evaluation for evaluation in evaluations}
        for evaluation in evaluations:
            candidate = next(
                (item for item in ranked if item.candidate_id == evaluation.candidate_id),
                None,
            )
            if candidate is None:
                continue
            base_score = candidate.score - candidate.score_breakdown.get("aiEntityResolution", 0)
            candidate.ai_verdict = evaluation.verdict
            candidate.contradictions = evaluation.contradictions
            candidate.missing_evidence = evaluation.missing_evidence
            if evaluation.verdict == "REJECT":
                candidate.score = 0
            else:
                verdict_adjustment = 15 if evaluation.verdict == "MATCH" else -5
                evidence_penalty = min(15, len(evaluation.missing_evidence) * 3)
                contradiction_penalty = min(40, len(evaluation.contradictions) * 12)
                candidate.score = round(
                    max(
                        0,
                        min(
                            100,
                            base_score
                            + verdict_adjustment
                            - evidence_penalty
                            - contradiction_penalty,
                        ),
                    ),
                    2,
                )
            candidate.confidence_score = round(candidate.score / 100, 4)
            candidate.score_breakdown["aiEntityResolution"] = round(
                candidate.score - base_score,
                2,
            )
            candidate.matched_fields = evaluation.matched_fields
            candidate.reason = evaluation.reason
        for candidate in ranked:
            if candidate.candidate_id not in by_id:
                candidate.ai_verdict = None
                candidate.confidence_score = round(candidate.score / 100, 4)
        return sorted(ranked, key=lambda item: (-item.score, item.candidate_id or ""))

    @staticmethod
    def _populate_result_contract(
        ranked: list[LocationCandidate],
        used_agent_resolution: bool,
    ) -> None:
        for candidate in ranked:
            candidate.place_name = candidate.place_name or candidate.name
            candidate.normalized_address = candidate.normalized_address or candidate.address
            ai_judged = "aiEntityResolution" in candidate.score_breakdown
            if not candidate.confidence_score and not ai_judged:
                candidate.confidence_score = round(candidate.score / 100, 4)
            if not candidate.matched_fields and not ai_judged:
                candidate.matched_fields = [
                    key
                    for key, value in candidate.score_breakdown.items()
                    if value > 0 and key not in {"aiEntityResolution"}
                ]
            if not candidate.reason:
                origin = "AI และหลายแหล่งข้อมูล" if used_agent_resolution else "การเทียบหลายฟิลด์"
                candidate.reason = f"จัดอันดับด้วย{origin}จากชื่อ เบอร์โทร ที่อยู่ บริษัท/สาขา และบริบทพื้นที่"

    @staticmethod
    def _attach_evidence_verified_phone(
        parsed: ParsedLocation,
        ranked: list[LocationCandidate],
        evidence: list[ResearchEvidence],
    ) -> None:
        phone = parsed.phone
        top = ranked[0] if ranked else None
        evidence_text = " ".join(f"{item.title} {item.url}" for item in evidence)
        evidence_digits = "".join(character for character in evidence_text if character.isdigit())
        strong_location_match = bool(
            top
            and {
                "house_number",
                "subdistrict",
                "district",
                "province",
                "postal_code",
                "provider_consensus",
            }.intersection(top.matched_fields)
        )
        if (
            phone
            and top
            and not top.phone
            and top.confidence_score >= 0.88
            and strong_location_match
            and phone in evidence_digits
        ):
            top.phone = phone
            top.matched_fields = list(dict.fromkeys([*top.matched_fields, "phone"]))
            original_reason = top.reason or "ข้อมูลชื่อและที่อยู่ของสถานที่ตรงกัน"
            original_reason = original_reason.replace(
                "ไม่มีข้อมูลเบอร์โทร", "Map Provider ไม่ได้ส่งข้อมูลเบอร์โทร"
            ).replace("ไม่มีเบอร์โทร", "Map Provider ไม่ได้ส่งเบอร์โทร")
            top.reason = (
                f"{original_reason} เบอร์ {phone} ตรงกับ raw input และปรากฏในหลักฐานเว็บที่ AI ตรวจสอบ"
            )

    def _rank(
        self,
        parsed: ParsedLocation,
        request: ResolveRequest,
        internal: list[LocationCandidate],
        external: list[LocationCandidate],
    ) -> tuple[list[LocationCandidate], list[LocationCandidate]]:
        deduplicated = self.deduplicator.deduplicate(self.aggregator.aggregate(internal, external))
        enriched = self.enricher.enrich(deduplicated)
        ranked = [
            self.scorer.score(parsed, candidate, request.context)
            for candidate in self.filter.filter(parsed, enriched)
        ]
        ranked.sort(key=lambda item: item.score, reverse=True)
        return ranked, deduplicated

    async def _enrich_ranked(
        self,
        parsed: ParsedLocation,
        request: ResolveRequest,
        ranked: list[LocationCandidate],
    ) -> list[LocationCandidate]:
        selected = ranked[: self.settings.google_place_details_limit]
        for provider in self.providers:
            try:
                await provider.enrich(selected)
            except Exception as exc:
                logger.warning(
                    "provider_enrichment_failed provider=%s error=%s",
                    provider.name,
                    type(exc).__name__,
                )
        output = [self.scorer.score(parsed, candidate, request.context) for candidate in ranked]
        output.sort(key=lambda item: item.score, reverse=True)
        return output

    def _combine_provider_statuses(
        self,
        first: list[ProviderStatus],
        second: list[ProviderStatus],
    ) -> list[ProviderStatus]:
        active = [status for status in [*first, *second] if status.status != "disabled"]
        return [*self._aggregate_provider_statuses(active), *self._disabled_provider_statuses()]

    async def _search_external(
        self, queries: list[str]
    ) -> tuple[list[LocationCandidate], list[ProviderStatus]]:
        if not self.providers:
            return [], self._disabled_provider_statuses()
        tasks = [
            self._cached_search(provider, query)
            for provider in self.providers
            for query in queries[: provider.max_queries]
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        candidates: list[LocationCandidate] = []
        statuses: list[ProviderStatus] = []
        for result in results:
            if isinstance(result, BaseException):
                statuses.append(
                    ProviderStatus(
                        provider="unknown",
                        status="error",
                        latency_ms=0,
                        error=type(result).__name__,
                    )
                )
            else:
                found, status = result
                candidates.extend(found)
                statuses.append(status)
        statuses = self._aggregate_provider_statuses(statuses)
        statuses.extend(self._disabled_provider_statuses())
        return candidates, statuses

    @staticmethod
    def _aggregate_provider_statuses(statuses: list[ProviderStatus]) -> list[ProviderStatus]:
        output: list[ProviderStatus] = []
        for provider in dict.fromkeys(status.provider for status in statuses):
            group = [status for status in statuses if status.provider == provider]
            successes = sum(status.status in {"success", "partial"} for status in group)
            if all(status.status == "success" for status in group):
                state = "success"
            elif successes:
                state = "partial"
            else:
                state = "error"
            errors = list(dict.fromkeys(status.error for status in group if status.error))
            output.append(
                ProviderStatus(
                    provider=provider,
                    status=state,
                    latency_ms=max((status.latency_ms for status in group), default=0),
                    error=", ".join(errors) or None,
                    cached=all(status.cached for status in group),
                )
            )
        return output

    def _disabled_provider_statuses(self) -> list[ProviderStatus]:
        configured = {provider.name for provider in self.providers}
        return [
            ProviderStatus(
                provider=name,
                status="disabled",
                latency_ms=0,
                error="Provider credentials or endpoint not configured",
            )
            for name in ("google", "here", "longdo", "openstreetmap")
            if name not in configured
        ]

    async def _cached_search(
        self, provider: MapProvider, query: str
    ) -> tuple[list[LocationCandidate], ProviderStatus]:
        key = f"{provider.name}:{' '.join(query.casefold().split())}"
        cached = await self.cache.get(key)
        if cached is not None:
            return [item.model_copy(deep=True) for item in cached], ProviderStatus(
                provider=provider.name, status="success", latency_ms=0, cached=True
            )
        task = self._searches.get(key)
        if task is None:
            task = asyncio.create_task(self._fetch_search(provider, query, key))
            self._searches[key] = task
            task.add_done_callback(lambda _: self._searches.pop(key, None))
        found, status = await asyncio.shield(task)
        return [item.model_copy(deep=True) for item in found], status.model_copy(deep=True)

    async def _fetch_search(
        self, provider: MapProvider, query: str, key: str
    ) -> tuple[list[LocationCandidate], ProviderStatus]:
        started = time.perf_counter()
        if not await self._circuit_available(provider.name):
            return [], ProviderStatus(
                provider=provider.name,
                status="error",
                latency_ms=0,
                error="CircuitOpen",
            )
        try:
            async with self.provider_semaphore:
                async with asyncio.timeout(
                    provider.search_timeout(self.settings.google_request_timeout_seconds)
                ):
                    found = await provider.search(query)
            if found:
                await self.cache.set(
                    key,
                    [item.model_copy(deep=True) for item in found],
                    self.settings.cache_ttl_seconds,
                )
            await self._record_provider_success(provider.name)
            return found, ProviderStatus(
                provider=provider.name,
                status="success",
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )
        except Exception as exc:
            await self._record_provider_failure(provider.name)
            logger.warning(
                "provider_search_failed provider=%s error=%s", provider.name, type(exc).__name__
            )
            return [], ProviderStatus(
                provider=provider.name,
                status="error",
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                error=type(exc).__name__,
            )

    async def _circuit_available(self, provider: str) -> bool:
        async with self.circuit_lock:
            open_until = self.circuit_open_until.get(provider, 0)
            if open_until <= time.monotonic():
                self.circuit_open_until.pop(provider, None)
                return True
            return False

    async def _record_provider_success(self, provider: str) -> None:
        async with self.circuit_lock:
            self.circuit_failures.pop(provider, None)
            self.circuit_open_until.pop(provider, None)

    async def _record_provider_failure(self, provider: str) -> None:
        async with self.circuit_lock:
            failures = self.circuit_failures.get(provider, 0) + 1
            self.circuit_failures[provider] = failures
            if failures >= self.settings.provider_circuit_failure_threshold:
                self.circuit_open_until[provider] = (
                    time.monotonic() + self.settings.provider_circuit_cooldown_seconds
                )

    async def confirm(self, request: ConfirmRequest, database: MongoDatabase) -> ConfirmResponse:
        if not request.candidate.confirmation_token or not hmac.compare_digest(
            request.candidate.confirmation_token,
            self._sign(request.candidate, request.raw_input),
        ):
            raise ValueError("candidate was not issued by this service")
        parsed = self.parser.parse(request.raw_input)
        location = await LocationRepository(database).confirm(
            request.raw_input, request.candidate, parsed
        )
        logger.info(
            "location_confirmed location_id=%s alias=%s",
            location_id(location),
            mask_phones(parsed.normalized_input),
        )
        return ConfirmResponse(
            location_id=location_id(location),
            verification_method=str(location["verification_method"]),
            message="บันทึกสถานที่เรียบร้อยแล้ว",
        )

    async def close(self) -> None:
        if self.researcher:
            await self.researcher.close()
        if self.registry:
            await self.registry.close()

    def _sign(self, candidate: LocationCandidate, raw_input: str) -> str:
        value = json.dumps(
            {
                "rawInput": raw_input,
                "candidate": candidate.model_dump(
                    mode="json", exclude={"raw", "confirmation_token"}, by_alias=True
                ),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hmac.new(
            self.settings.signing_secret.encode(), value.encode(), hashlib.sha256
        ).hexdigest()

    @staticmethod
    def _log(
        response: ResolveResponse,
        request: ResolveRequest,
        started: float,
        before: int,
        after: int,
        queries: list[str] | None = None,
    ) -> None:
        parsed_log = response.parsed.model_dump(exclude={"raw_input"})
        parsed_log["phone"] = mask_phones(response.parsed.phone)
        parsed_log["normalized_input"] = mask_phones(response.parsed.normalized_input)
        logger.info(
            "resolution_complete requestId=%s rawInput=%s normalizedInput=%s parserResult=%s "
            "queriesGenerated=%s providersUsed=%s providerLatency=%s providerErrors=%s "
            "candidateCountBeforeDedup=%s candidateCountAfterDedup=%s bestCandidate=%s "
            "score=%s confidence=%s researchStatus=%s researchQueries=%s totalLatencyMs=%.2f",
            response.request_id,
            mask_phones(request.input),
            mask_phones(response.parsed.normalized_input),
            parsed_log,
            queries or [],
            [status.provider for status in response.providers],
            [status.latency_ms for status in response.providers],
            [status.error for status in response.providers if status.error],
            before,
            after,
            response.best_match.provider_place_id if response.best_match else None,
            response.best_match.score if response.best_match else None,
            response.status,
            response.research.status,
            response.research.query_hints,
            (time.perf_counter() - started) * 1000,
        )

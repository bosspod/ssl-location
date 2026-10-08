import re
from datetime import UTC, datetime
from typing import Any

from bson import ObjectId
from rapidfuzz import fuzz

from app.database import MongoDatabase
from app.schemas import LocationCandidate, ParsedLocation
from app.services.normalizer import LocationNormalizer
from app.utils import compact_text, string_similarity

LocationDocument = dict[str, Any]


def search_terms(*values: str | None) -> list[str]:
    terms: set[str] = set()
    for value in values:
        if not value:
            continue
        normalized = compact_text(value)
        if not normalized:
            continue
        terms.add(normalized)
        terms.update(
            compact_text(part)
            for part in re.split(r"[^\w\u0E00-\u0E7F]+", value.casefold())
            if len(compact_text(part)) >= 2
        )
        terms.update(normalized[index : index + 3] for index in range(max(0, len(normalized) - 2)))
    return sorted(terms)


class LocationRepository:
    def __init__(self, database: MongoDatabase) -> None:
        self.collection = database.locations

    async def search(self, parsed: ParsedLocation, limit: int = 10) -> list[LocationCandidate]:
        normalized = compact_text(parsed.normalized_input)
        clauses: list[LocationDocument] = [
            {"normalized_address": normalized},
            {"aliases.normalized_alias": normalized},
        ]
        if parsed.phone:
            clauses.append({"phone": parsed.phone})
        if parsed.postal_code:
            clauses.append({"postal_code": parsed.postal_code})
        if parsed.place_name:
            clauses.append(
                {"place_name": {"$regex": re.escape(parsed.place_name), "$options": "i"}}
            )
        documents = await self._documents({"$or": clauses}, limit * 3)
        terms = search_terms(parsed.place_name, parsed.normalized_input)
        if terms:
            term_documents = await self._term_documents(terms, limit * 3)
            seen = {document["_id"] for document in documents}
            documents.extend(document for document in term_documents if document["_id"] not in seen)
        if len(documents) < limit:
            fallback = await self._documents({}, 500)
            seen = {document["_id"] for document in documents}
            documents.extend(document for document in fallback if document["_id"] not in seen)

        scored: list[tuple[float, LocationDocument]] = []
        for document in documents:
            aliases = document.get("aliases", [])
            choices = [
                compact_text(document.get("place_name")),
                document.get("normalized_address", ""),
                *(alias.get("normalized_alias", "") for alias in aliases),
            ]
            fuzzy = max((fuzz.ratio(normalized, choice) for choice in choices if choice), default=0)
            if parsed.phone and parsed.phone == document.get("phone"):
                fuzzy = max(fuzzy, 100)
            exact_alias = any(normalized == alias.get("normalized_alias") for alias in aliases)
            exact_address = normalized == document.get("normalized_address")
            if fuzzy >= 55 or exact_alias:
                ranked_document = dict(document)
                ranked_document["_exact_master_match"] = exact_alias or exact_address
                scored.append((100 if exact_alias else float(fuzzy), ranked_document))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            self._candidate(document, parsed.normalized_input, score)
            for score, document in scored[:limit]
        ]

    async def _documents(self, query: LocationDocument, limit: int) -> list[LocationDocument]:
        output: list[LocationDocument] = []
        async for document in self.collection.find(query).limit(limit):
            output.append(document)
        return output

    async def _term_documents(self, terms: list[str], limit: int) -> list[LocationDocument]:
        output: list[LocationDocument] = []
        pipeline: list[LocationDocument] = [
            {"$match": {"search_terms": {"$in": terms}}},
            {
                "$set": {
                    "_term_hits": {
                        "$size": {
                            "$setIntersection": [
                                {"$ifNull": ["$search_terms", []]},
                                terms,
                            ]
                        }
                    }
                }
            },
            {"$sort": {"_term_hits": -1, "last_verified_at": -1}},
            {"$limit": limit},
            {"$unset": "_term_hits"},
        ]
        async for document in await self.collection.aggregate(pipeline):
            output.append(document)
        return output

    @staticmethod
    def _candidate(
        document: LocationDocument, query: str, master_match: float
    ) -> LocationCandidate:
        coordinates = document["location"]["coordinates"]
        return LocationCandidate(
            provider="location_master",
            provider_place_id=document.get("provider_place_id"),
            name=document.get("place_name"),
            address=document.get("standardized_address"),
            phone=document.get("phone"),
            latitude=float(coordinates[1]),
            longitude=float(coordinates[0]),
            house_number=document.get("house_number"),
            moo=document.get("moo"),
            village=document.get("village"),
            building=document.get("building"),
            soi=document.get("soi"),
            road=document.get("road"),
            subdistrict=document.get("subdistrict"),
            district=document.get("district"),
            province=document.get("province"),
            postal_code=document.get("postal_code"),
            source_query=query,
            sources=list(dict.fromkeys(["location_master", *document.get("sources", [])])),
            provider_place_ids=document.get("provider_place_ids", {}),
            score_breakdown={
                "masterMatch": master_match,
                "exactMasterMatch": (1 if document.get("_exact_master_match") else 0),
            },
            verification_status=document.get("verification_method"),
        )

    async def confirm(
        self, raw_input: str, candidate: LocationCandidate, parsed: ParsedLocation
    ) -> LocationDocument:
        now = datetime.now(UTC)
        normalized_alias = compact_text(LocationNormalizer().normalize(raw_input))
        existing: LocationDocument | None = None
        if candidate.provider_place_id:
            existing = await self.collection.find_one(
                {
                    "$or": [
                        {f"provider_place_ids.{candidate.provider}": candidate.provider_place_id},
                        {"provider_place_id": candidate.provider_place_id},
                    ]
                }
            )
        if existing is None:
            nearby = await self._documents(
                {
                    "location": {
                        "$near": {
                            "$geometry": {
                                "type": "Point",
                                "coordinates": [candidate.longitude, candidate.latitude],
                            },
                            "$maxDistance": 10,
                        }
                    }
                },
                10,
            )
            existing = next(
                (
                    document
                    for document in nearby
                    if max(
                        string_similarity(candidate.name, document.get("place_name")),
                        string_similarity(candidate.address, document.get("standardized_address")),
                    )
                    >= 0.8
                ),
                None,
            )

        alias = {
            "alias": raw_input.strip(),
            "normalized_alias": normalized_alias,
            "created_at": now,
        }
        if existing is None:
            document: LocationDocument = {
                "place_name": candidate.name or parsed.place_name,
                "standardized_address": candidate.address or parsed.normalized_input,
                "normalized_address": compact_text(candidate.address or parsed.normalized_input),
                "location": {
                    "type": "Point",
                    "coordinates": [candidate.longitude, candidate.latitude],
                },
                "phone": candidate.phone or parsed.phone,
                "house_number": candidate.house_number or parsed.house_number,
                "moo": candidate.moo or parsed.moo,
                "village": candidate.village or parsed.village,
                "building": candidate.building or parsed.building,
                "soi": candidate.soi or parsed.soi,
                "road": candidate.road or parsed.road,
                "subdistrict": candidate.subdistrict or parsed.subdistrict,
                "district": candidate.district or parsed.district,
                "province": candidate.province or parsed.province,
                "postal_code": candidate.postal_code or parsed.postal_code,
                "confidence_score": max(candidate.score, 85),
                "verification_method": "USER_CONFIRMED",
                "successful_delivery_count": 0,
                "actual_delivery_location": None,
                "actual_delivery_latitude": None,
                "actual_delivery_longitude": None,
                "delivery_verification_status": None,
                "aliases": [alias],
                "search_terms": search_terms(
                    candidate.name or parsed.place_name,
                    candidate.address or parsed.normalized_input,
                    normalized_alias,
                ),
                "provider_place_ids": {
                    **candidate.provider_place_ids,
                    **(
                        {candidate.provider: candidate.provider_place_id}
                        if candidate.provider_place_id
                        else {}
                    ),
                },
                "sources": candidate.sources or [candidate.provider],
                "created_at": now,
                "updated_at": now,
                "last_verified_at": now,
            }
            result = await self.collection.insert_one(document)
            document["_id"] = result.inserted_id
            return document

        aliases = existing.get("aliases", [])
        new_alias = bool(normalized_alias) and not any(
            item.get("normalized_alias") == normalized_alias for item in aliases
        )
        update: LocationDocument = {
            "$set": {
                "verification_method": "USER_CONFIRMED",
                "confidence_score": min(
                    100,
                    float(existing.get("confidence_score", 0)) + (2 if new_alias else 0),
                ),
                "updated_at": now,
                "last_verified_at": now,
                **{
                    f"provider_place_ids.{provider}": identifier
                    for provider, identifier in candidate.provider_place_ids.items()
                },
            },
            "$addToSet": {"sources": {"$each": candidate.sources or [candidate.provider]}},
        }
        if candidate.provider_place_id:
            update["$set"][f"provider_place_ids.{candidate.provider}"] = candidate.provider_place_id
        if new_alias:
            update["$push"] = {"aliases": alias}
            update["$addToSet"]["search_terms"] = {"$each": search_terms(normalized_alias)}
        await self.collection.update_one({"_id": existing["_id"]}, update)
        existing.update(update["$set"])
        return existing


def location_id(document: LocationDocument) -> str:
    identifier = document.get("_id")
    if not isinstance(identifier, ObjectId):
        raise RuntimeError("location document is missing a valid ObjectId")
    return str(identifier)

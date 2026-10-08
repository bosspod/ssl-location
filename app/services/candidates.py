import hashlib
from copy import deepcopy

from app.schemas import LocationCandidate, ParsedLocation
from app.utils import (
    admin_area_similarity,
    calculate_distance_meters,
    compact_text,
    place_name_similarity,
    string_similarity,
)


class CandidateAggregator:
    def aggregate(self, *groups: list[LocationCandidate]) -> list[LocationCandidate]:
        return [candidate for group in groups for candidate in group]


class CandidateDeduplicator:
    def __init__(self, radius_meters: float = 50, name_threshold: float = 0.65) -> None:
        self.radius_meters = radius_meters
        self.name_threshold = name_threshold

    def deduplicate(self, candidates: list[LocationCandidate]) -> list[LocationCandidate]:
        merged: list[LocationCandidate] = []
        for incoming in candidates:
            duplicate = next((item for item in merged if self._same(item, incoming)), None)
            if duplicate is None:
                item = deepcopy(incoming)
                item.sources = item.sources or [item.provider]
                merged.append(item)
            else:
                self._merge(duplicate, incoming)
        for candidate in merged:
            candidate.candidate_id = self._candidate_id(candidate)
        return merged

    @staticmethod
    def _candidate_id(candidate: LocationCandidate) -> str:
        # Content and consensus change during enrichment; provider identity does not.
        primary_id = candidate.provider_place_id or candidate.provider_place_ids.get(
            candidate.provider
        )
        if primary_id:
            return hashlib.sha256(f"{candidate.provider}:{primary_id}".encode()).hexdigest()[:16]
        provider_ids = "|".join(
            f"{provider}:{identifier}"
            for provider, identifier in sorted(candidate.provider_place_ids.items())
        )
        identity = "|".join(
            [
                provider_ids,
                compact_text(candidate.name),
                compact_text(candidate.address),
                f"{candidate.latitude:.6f}",
                f"{candidate.longitude:.6f}",
            ]
        )
        return hashlib.sha256(identity.encode()).hexdigest()[:16]

    def _same(self, left: LocationCandidate, right: LocationCandidate) -> bool:
        if (
            left.provider == right.provider
            and left.provider_place_id
            and right.provider_place_id
            and left.provider_place_id == right.provider_place_id
        ):
            return True
        distance = calculate_distance_meters(
            left.latitude, left.longitude, right.latitude, right.longitude
        )
        if distance >= self.radius_meters:
            return False
        address_similarity = string_similarity(left.address, right.address)
        if not left.name or not right.name:
            return address_similarity >= 0.75
        return (
            place_name_similarity(left.name, right.name) >= self.name_threshold
            or address_similarity >= 0.75
        )

    @staticmethod
    def _merge(target: LocationCandidate, source: LocationCandidate) -> None:
        target.sources = list(
            dict.fromkeys([*target.sources, *(source.sources or [source.provider])])
        )
        target.provider_place_ids.update(source.provider_place_ids)
        if target.provider_place_id:
            target.provider_place_ids.setdefault(target.provider, target.provider_place_id)
        if source.provider_place_id:
            target.provider_place_ids.setdefault(source.provider, source.provider_place_id)
        for field in (
            "provider_place_id",
            "name",
            "address",
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
            "category",
            "business_status",
            "website",
        ):
            if not getattr(target, field) and getattr(source, field):
                setattr(target, field, getattr(source, field))
        target.score_breakdown.update(source.score_breakdown)


class CandidateFilter:
    def filter(
        self, parsed: ParsedLocation, candidates: list[LocationCandidate]
    ) -> list[LocationCandidate]:
        output: list[LocationCandidate] = []
        for candidate in candidates:
            if self._is_generic_noise(parsed, candidate):
                continue
            reject = False
            for field in ("province", "postal_code", "district"):
                expected, actual = getattr(parsed, field), getattr(candidate, field)
                confidence = parsed.entity_confidence.get(field, 0)
                if expected and actual and confidence >= 0.9:
                    threshold = 0.8 if field != "postal_code" else 1.0
                    similarity = (
                        string_similarity(expected, actual)
                        if field == "postal_code"
                        else admin_area_similarity(expected, actual)
                    )
                    if similarity < threshold:
                        reject = True
                        break
            if not reject:
                output.append(candidate)
        return output

    @staticmethod
    def _is_generic_noise(parsed: ParsedLocation, candidate: LocationCandidate) -> bool:
        if not (parsed.company_name or parsed.place_name):
            return False
        has_anchor = bool(candidate.phone or candidate.house_number)
        category = (candidate.category or "").casefold()
        category_tokens = set(
            category.replace("_", " ").replace("/", " ").replace(",", " ").split()
        )
        geographic_types = {
            "route",
            "road",
            "street",
            "locality",
            "district",
            "province",
            "administrative",
            "political",
            "country",
            "state",
            "county",
        }
        if category_tokens & geographic_types and not has_anchor:
            return True
        normalized_category = category.replace("_", " ").replace("/", " ").replace(",", " ")
        unrelated_public_types = {
            "airport",
            "aerodrome",
            "bus station",
            "train station",
            "transit station",
            "tourist attraction",
        }
        if (
            parsed.company_name
            and any(item in normalized_category for item in unrelated_public_types)
            and not has_anchor
        ):
            identities = [parsed.company_name, *parsed.aliases]
            if (
                max(
                    (
                        place_name_similarity(identity, candidate.name)
                        for identity in identities
                        if identity
                    ),
                    default=0.0,
                )
                < 0.7
            ):
                return True
        name = (candidate.name or "").strip().casefold()
        thai_geographic_prefixes = (
            "ถนน",
            "ตำบล",
            "แขวง",
            "อำเภอ",
            "เขต",
            "จังหวัด",
            "ประเทศไทย",
        )
        english_geographic_names = {"road", "district", "province", "thailand"}
        first_word = name.split(maxsplit=1)[0] if name else ""
        if name and (
            any(name.startswith(item) for item in thai_geographic_prefixes)
            or first_word in english_geographic_names
        ):
            return not has_anchor
        if not candidate.name and not has_anchor:
            address = compact_text(candidate.address)
            return len(address) < 20
        return False

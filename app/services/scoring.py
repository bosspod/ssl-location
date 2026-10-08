from dataclasses import dataclass

from app.schemas import ConfidenceStatus, GeographicContext, LocationCandidate, ParsedLocation
from app.utils import (
    admin_area_similarity,
    calculate_distance_meters,
    normalize_phone,
    place_name_similarity,
    string_similarity,
)


@dataclass(frozen=True)
class ScoringProfile:
    """Configurable evidence weights; missing evidence never inflates a score."""

    phone: float = 50
    phone_only: float = 90
    place_name: float = 60
    company_name: float = 60
    alias: float = 18
    address: float = 25
    house_number: float = 25
    village_building: float = 8
    soi: float = 7
    road: float = 8
    subdistrict: float = 10
    district: float = 10
    province: float = 8
    postal_code: float = 12
    branch_site: float = 10
    area_context: float = 4
    landmark: float = 5
    category: float = 5
    provider_consensus: float = 8
    location_history: float = 5
    master_match: float = 10
    route_context: float = 4
    phone_contradiction: float = 35
    house_number_contradiction: float = 22
    postal_code_contradiction: float = 20
    province_contradiction: float = 25
    district_contradiction: float = 15
    entity_type_contradiction: float = 35


class GeographicValidationService:
    def score(self, candidate: LocationCandidate, context: GeographicContext | None) -> float:
        if not context:
            return 0.0
        if context.province and candidate.province:
            return (
                1.0 if admin_area_similarity(context.province, candidate.province) >= 0.8 else -1.0
            )
        points = [
            point
            for point in (context.previous_location, context.next_location, context.depot_location)
            if point
        ]
        if not points:
            return 0.0
        closest = min(
            calculate_distance_meters(candidate.latitude, candidate.longitude, point[0], point[1])
            for point in points
        )
        return max(-1.0, 1.0 - closest / 100_000)


class LocationScoringService:
    def __init__(
        self,
        geography: GeographicValidationService | None = None,
        profile: ScoringProfile | None = None,
    ) -> None:
        self.geography = geography or GeographicValidationService()
        self.profile = profile or ScoringProfile()

    def score(
        self,
        parsed: ParsedLocation,
        candidate: LocationCandidate,
        context: GeographicContext | None = None,
    ) -> LocationCandidate:
        profile = self.profile
        master_match = candidate.score_breakdown.get("masterMatch", 0)
        exact_master_match = candidate.score_breakdown.get("exactMasterMatch", 0) > 0
        candidate_text = " ".join(filter(None, [candidate.name, candidate.address]))
        address_target = self._address_target(parsed)
        signals: dict[str, tuple[float, float]] = {}

        if parsed.phone:
            phone_weight = profile.phone_only if self._is_phone_only(parsed) else profile.phone
            signals["phone"] = (
                phone_weight,
                1.0 if normalize_phone(candidate.phone) == parsed.phone else 0.0,
            )
        if parsed.place_name:
            signals["placeName"] = (
                profile.place_name,
                self._identity_similarity(parsed.place_name, candidate.name),
            )
        if parsed.company_name:
            signals["companyName"] = (
                profile.company_name,
                self._identity_similarity(parsed.company_name, candidate.name),
            )
        if parsed.aliases:
            signals["alias"] = (
                profile.alias,
                max(self._identity_similarity(alias, candidate.name) for alias in parsed.aliases),
            )
        if address_target:
            signals["address"] = (
                profile.address,
                string_similarity(address_target, candidate.address),
            )
        self._add(
            signals,
            "houseNumber",
            profile.house_number,
            self._exact(parsed.house_number, candidate.house_number),
            parsed.house_number,
        )
        self._add(
            signals,
            "villageBuilding",
            profile.village_building,
            max(
                string_similarity(parsed.village, candidate.village or candidate.address),
                string_similarity(parsed.building, candidate.building or candidate.address),
            ),
            parsed.village or parsed.building,
        )
        self._add(
            signals,
            "soi",
            profile.soi,
            string_similarity(parsed.soi, candidate.soi or candidate.address),
            parsed.soi,
        )
        self._add(
            signals,
            "road",
            profile.road,
            string_similarity(parsed.road, candidate.road or candidate.address),
            parsed.road,
        )
        self._add(
            signals,
            "subdistrict",
            profile.subdistrict,
            admin_area_similarity(parsed.subdistrict, candidate.subdistrict),
            parsed.subdistrict,
        )
        self._add(
            signals,
            "district",
            profile.district,
            admin_area_similarity(parsed.district, candidate.district),
            parsed.district,
        )
        self._add(
            signals,
            "province",
            profile.province,
            admin_area_similarity(parsed.province, candidate.province),
            parsed.province,
        )
        self._add(
            signals,
            "postalCode",
            profile.postal_code,
            self._exact(parsed.postal_code, candidate.postal_code),
            parsed.postal_code,
        )
        branch_site = parsed.branch_name or parsed.site_name
        self._add(
            signals,
            "branchSite",
            profile.branch_site,
            max(
                self._identity_similarity(branch_site, candidate_text),
                string_similarity(branch_site, candidate_text),
            ),
            branch_site,
        )
        self._add(
            signals,
            "areaContext",
            profile.area_context,
            string_similarity(parsed.area_context, candidate_text),
            parsed.area_context,
        )
        self._add(
            signals,
            "landmark",
            profile.landmark,
            string_similarity(parsed.landmark, candidate_text),
            parsed.landmark,
        )
        if parsed.company_name or parsed.place_name:
            signals["category"] = (profile.category, self._category_consistency(parsed, candidate))
        signals["providerConsensus"] = (
            profile.provider_consensus,
            min(1.0, max(0, len(candidate.sources) - 1) / 2),
        )
        signals["locationHistory"] = (
            profile.location_history,
            1.0 if "location_master" in candidate.sources else 0.0,
        )
        if master_match > 0:
            signals["masterMatch"] = (profile.master_match, min(1.0, master_match / 100))

        breakdown = {
            key: round(weight * max(0.0, min(1.0, similarity)), 2)
            for key, (weight, similarity) in signals.items()
        }
        if context:
            breakdown["geographicContext"] = round(
                self.geography.score(candidate, context) * profile.route_context, 2
            )
        breakdown.update(self._contradiction_penalties(parsed, candidate))
        if exact_master_match:
            breakdown["exactMasterMatch"] = 15
        candidate.score_breakdown = breakdown
        score = max(0.0, min(100.0, sum(breakdown.values())))
        if exact_master_match:
            score = max(95.0, score)
        candidate.score = round(score, 2)
        candidate.confidence_score = round(candidate.score / 100, 4)
        return candidate

    def _contradiction_penalties(
        self, parsed: ParsedLocation, candidate: LocationCandidate
    ) -> dict[str, float]:
        profile = self.profile
        penalties: dict[str, float] = {}
        expected_phone = normalize_phone(parsed.phone)
        actual_phone = normalize_phone(candidate.phone)
        if expected_phone and actual_phone and expected_phone != actual_phone:
            penalties["phoneContradiction"] = -profile.phone_contradiction
        if (
            parsed.house_number
            and candidate.house_number
            and not self._exact(parsed.house_number, candidate.house_number)
        ):
            penalties["houseNumberContradiction"] = -profile.house_number_contradiction
        if (
            parsed.postal_code
            and candidate.postal_code
            and parsed.postal_code != candidate.postal_code
        ):
            penalties["postalCodeContradiction"] = -profile.postal_code_contradiction
        if (
            parsed.province
            and candidate.province
            and admin_area_similarity(parsed.province, candidate.province) < 0.5
        ):
            penalties["provinceContradiction"] = -profile.province_contradiction
        if (
            parsed.district
            and candidate.district
            and admin_area_similarity(parsed.district, candidate.district) < 0.5
        ):
            penalties["districtContradiction"] = -profile.district_contradiction
        if self._wrong_entity_type(parsed, candidate):
            penalties["entityTypeContradiction"] = -profile.entity_type_contradiction
        return penalties

    @staticmethod
    def _add(
        signals: dict[str, tuple[float, float]],
        key: str,
        weight: float,
        similarity: float,
        expected: str | None,
    ) -> None:
        if expected:
            signals[key] = (weight, similarity)

    @staticmethod
    def _address_target(parsed: ParsedLocation) -> str:
        return " ".join(
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

    @staticmethod
    def _is_phone_only(parsed: ParsedLocation) -> bool:
        return bool(
            parsed.phone
            and not any(
                [
                    parsed.place_name,
                    parsed.company_name,
                    parsed.house_number,
                    parsed.road,
                    parsed.subdistrict,
                    parsed.district,
                    parsed.province,
                ]
            )
        )

    @staticmethod
    def _category_consistency(parsed: ParsedLocation, candidate: LocationCandidate) -> float:
        category = (candidate.category or "").casefold()
        if not category:
            return 0.0
        category_tokens = set(
            category.replace("_", " ").replace("/", " ").replace(",", " ").split()
        )
        geographic = {
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
        if category_tokens & geographic:
            return 0.0
        return 1.0 if parsed.company_name or parsed.place_name else 0.5

    @classmethod
    def _wrong_entity_type(cls, parsed: ParsedLocation, candidate: LocationCandidate) -> bool:
        return bool(
            (parsed.company_name or parsed.place_name)
            and candidate.category
            and cls._category_consistency(parsed, candidate) == 0
        )

    @staticmethod
    def _exact(expected: str | None, actual: str | None) -> float:
        return (
            1.0
            if expected and actual and expected.casefold().strip() == actual.casefold().strip()
            else 0.0
        )

    @staticmethod
    def _identity_similarity(expected: str | None, actual: str | None) -> float:
        expected_key = "".join(
            character.casefold() for character in (expected or "") if character.isalnum()
        )
        actual_key = "".join(
            character.casefold() for character in (actual or "") if character.isalnum()
        )
        if len(expected_key) >= 3 and expected_key in actual_key:
            return 1.0
        return place_name_similarity(expected, actual)


class ConfidenceService:
    def __init__(self, minimum_margin: float = 10) -> None:
        self.minimum_margin = minimum_margin

    def classify(self, ranked: list[LocationCandidate]) -> ConfidenceStatus:
        if not ranked:
            return ConfidenceStatus.NO_RESULT
        top = ranked[0].score
        if len(ranked) > 1 and top - ranked[1].score < self.minimum_margin:
            return ConfidenceStatus.CONFLICT
        if top >= 95 and "location_master" in ranked[0].sources:
            return ConfidenceStatus.EXACT
        if top >= 85:
            return ConfidenceStatus.HIGH
        if top >= 60:
            return ConfidenceStatus.MEDIUM
        return ConfidenceStatus.LOW

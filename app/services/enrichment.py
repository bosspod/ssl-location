from app.schemas import LocationCandidate
from app.services.parser import LocationParserService
from app.utils import normalize_phone


class CandidateEnrichmentService:
    """Fill missing structured data from provider text without changing coordinates."""

    def __init__(self, parser: LocationParserService | None = None) -> None:
        self.parser = parser or LocationParserService()

    def enrich(self, candidates: list[LocationCandidate]) -> list[LocationCandidate]:
        for candidate in candidates:
            if candidate.phone:
                candidate.phone = normalize_phone(candidate.phone)
            if not candidate.address:
                continue
            parsed = self.parser.parse(candidate.address)
            for field in (
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
            ):
                if not getattr(candidate, field):
                    setattr(candidate, field, getattr(parsed, field))
        return candidates

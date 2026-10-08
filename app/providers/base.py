from abc import ABC, abstractmethod

from app.schemas import LocationCandidate


class MapProviderError(RuntimeError):
    pass


class MapProvider(ABC):
    name: str
    max_queries: int | None = None

    def search_timeout(self, request_timeout: float) -> float:
        """Budget the entire search, including sequential pages and retries."""
        attempts = getattr(self, "max_retries", 0) + 1
        pages = getattr(self, "max_pages", 1)
        methods = 2 if self.name == "here" else 1
        backoff = sum(0.1 * 2**attempt for attempt in range(attempts - 1))
        return float(methods * pages * (request_timeout * attempts + backoff))

    @abstractmethod
    async def search(self, query: str) -> list[LocationCandidate]: ...

    async def enrich(
        self, candidates: list[LocationCandidate]
    ) -> list[LocationCandidate]:
        return candidates

    async def close(self) -> None:
        return None

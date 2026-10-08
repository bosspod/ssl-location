import asyncio
import time
from collections import deque
from typing import Any

import httpx

from app.providers.base import MapProvider, MapProviderError
from app.schemas import LocationCandidate
from app.utils import normalize_phone


class LongdoMapsProvider(MapProvider):
    name = "longdo"
    search_url = "https://search.longdo.com/mapsearch/json/search"

    def __init__(
        self,
        api_key: str,
        timeout: float = 4.0,
        result_limit: int = 20,
        max_retries: int = 1,
        rate_limit_per_minute: int = 60,
    ) -> None:
        self.api_key = api_key
        self.result_limit = result_limit
        self.max_retries = max_retries
        self.rate_limit_per_minute = rate_limit_per_minute
        self._request_times: deque[float] = deque()
        self._rate_lock = asyncio.Lock()
        self.client = httpx.AsyncClient(timeout=timeout)

    async def search(self, query: str) -> list[LocationCandidate]:
        try:
            response = await self._request(query)
            payload = response.json()
        except (MapProviderError, ValueError) as exc:
            raise MapProviderError("Longdo search failed") from exc
        items = payload.get("data") or payload.get("results") or []
        if not isinstance(items, list):
            raise MapProviderError("Longdo returned an unsupported response")
        output: list[LocationCandidate] = []
        for item in items:
            if isinstance(item, dict) and (candidate := self._map_item(item, query)):
                output.append(candidate)
        return output

    async def _request(self, query: str) -> httpx.Response:
        error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                await self._check_quota()
                response = await self.client.get(
                    self.search_url,
                    params={
                        "keyword": query,
                        "key": self.api_key,
                        "limit": self.result_limit,
                        "locale": "th",
                    },
                )
                response.raise_for_status()
                return response
            except httpx.HTTPError as exc:
                error = exc
                retryable = (
                    not isinstance(exc, httpx.HTTPStatusError)
                    or exc.response.status_code == 429
                    or exc.response.status_code >= 500
                )
                if attempt >= self.max_retries or not retryable:
                    break
                await asyncio.sleep(0.1 * (2**attempt))
        raise MapProviderError(f"Longdo request failed: {type(error).__name__}") from error

    async def _check_quota(self) -> None:
        now = time.monotonic()
        async with self._rate_lock:
            while self._request_times and self._request_times[0] <= now - 60:
                self._request_times.popleft()
            if len(self._request_times) >= self.rate_limit_per_minute:
                raise MapProviderError("Longdo provider rate limit reached")
            self._request_times.append(now)

    def _map_item(self, item: dict[str, Any], query: str) -> LocationCandidate | None:
        latitude = item.get("lat") or item.get("latitude")
        longitude = item.get("lon") or item.get("lng") or item.get("longitude")
        if latitude is None or longitude is None:
            return None
        identifier = str(item.get("id")) if item.get("id") is not None else None
        return LocationCandidate(
            provider=self.name,
            provider_place_id=identifier,
            provider_place_ids={self.name: identifier} if identifier else {},
            name=item.get("name") or item.get("title"),
            address=item.get("address") or item.get("description"),
            phone=normalize_phone(item.get("tel") or item.get("phone")),
            category=str(item.get("category") or item.get("type") or "") or None,
            latitude=float(latitude),
            longitude=float(longitude),
            subdistrict=item.get("subdistrict") or item.get("tambon"),
            district=item.get("district") or item.get("amphoe"),
            province=item.get("province"),
            postal_code=item.get("postcode") or item.get("postal_code"),
            source_query=query,
            sources=[self.name],
            raw=item,
        )

    async def close(self) -> None:
        await self.client.aclose()

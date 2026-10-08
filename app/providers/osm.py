import asyncio
import time
from typing import Any

import httpx

from app.providers.base import MapProvider, MapProviderError
from app.schemas import LocationCandidate


class OpenStreetMapProvider(MapProvider):
    name = "openstreetmap"

    def __init__(
        self,
        base_url: str,
        user_agent: str,
        timeout: float = 4.0,
        result_limit: int = 20,
        requests_per_second: float = 1.0,
        max_queries: int = 1,
    ) -> None:
        self.search_url = f"{base_url.rstrip('/')}/search"
        self.result_limit = min(result_limit, 40)
        self.minimum_interval = 1 / requests_per_second
        self.max_queries = max_queries
        self._last_request_at = 0.0
        self._rate_lock = asyncio.Lock()
        self.client = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": user_agent, "Accept-Language": "th,en;q=0.8"},
        )

    async def search(self, query: str) -> list[LocationCandidate]:
        await self._check_quota()
        try:
            response = await self.client.get(
                self.search_url,
                params={
                    "q": query,
                    "format": "jsonv2",
                    "addressdetails": 1,
                    "namedetails": 1,
                    "countrycodes": "th",
                    "accept-language": "th,en",
                    "limit": self.result_limit,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MapProviderError("OpenStreetMap search failed") from exc
        if not isinstance(payload, list):
            raise MapProviderError("OpenStreetMap returned an unsupported response")
        output: list[LocationCandidate] = []
        for item in payload:
            if isinstance(item, dict) and (candidate := self._map_item(item, query)):
                output.append(candidate)
        return output

    async def _check_quota(self) -> None:
        async with self._rate_lock:
            now = time.monotonic()
            if now - self._last_request_at < self.minimum_interval:
                raise MapProviderError("OpenStreetMap provider rate limit reached")
            self._last_request_at = now

    def _map_item(self, item: dict[str, Any], query: str) -> LocationCandidate | None:
        if item.get("lat") is None or item.get("lon") is None:
            return None
        address = item.get("address") or {}
        names = item.get("namedetails") or {}
        identifier = self._identifier(item)
        name = (
            names.get("name:th")
            or names.get("name")
            or item.get("name")
            or address.get(item.get("addresstype", ""))
        )
        return LocationCandidate(
            provider=self.name,
            provider_place_id=identifier,
            provider_place_ids={self.name: identifier} if identifier else {},
            name=name,
            address=item.get("display_name"),
            category="/".join(
                str(value)
                for value in [item.get("class"), item.get("type"), item.get("addresstype")]
                if value
            )
            or None,
            latitude=float(item["lat"]),
            longitude=float(item["lon"]),
            house_number=address.get("house_number"),
            road=address.get("road"),
            subdistrict=(
                address.get("subdistrict") or address.get("suburb") or address.get("quarter")
            ),
            district=(
                address.get("district")
                or address.get("city_district")
                or address.get("county")
                or address.get("city")
            ),
            province=address.get("state") or address.get("province"),
            postal_code=address.get("postcode"),
            source_query=query,
            sources=[self.name],
            raw=item,
        )

    @staticmethod
    def _identifier(item: dict[str, Any]) -> str | None:
        if item.get("osm_type") and item.get("osm_id") is not None:
            return f"{item['osm_type']}:{item['osm_id']}"
        if item.get("place_id") is not None:
            return str(item["place_id"])
        return None

    async def close(self) -> None:
        await self.client.aclose()

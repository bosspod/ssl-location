import asyncio
from typing import Any

import httpx

from app.providers.base import MapProvider, MapProviderError
from app.schemas import LocationCandidate
from app.utils import normalize_phone


class HereMapsProvider(MapProvider):
    name = "here"
    discover_url = "https://discover.search.hereapi.com/v1/discover"
    geocode_url = "https://geocode.search.hereapi.com/v1/geocode"

    def __init__(
        self,
        api_key: str,
        timeout: float = 4.0,
        result_limit: int = 20,
        max_retries: int = 1,
    ) -> None:
        self.api_key = api_key
        self.result_limit = result_limit
        self.max_retries = max_retries
        self.client = httpx.AsyncClient(timeout=timeout)

    async def _request(self, url: str, query: str) -> httpx.Response:
        error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = await self.client.get(
                    url,
                    params={
                        "q": query,
                        "apiKey": self.api_key,
                        "in": "countryCode:THA",
                        "lang": "th-TH",
                        "limit": self.result_limit,
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
        raise MapProviderError(f"HERE request failed: {type(error).__name__}") from error

    async def search(self, query: str) -> list[LocationCandidate]:
        output: list[LocationCandidate] = []
        errors = 0
        for url in (self.discover_url, self.geocode_url):
            try:
                response = await self._request(url, query)
                output.extend(self._map_items(response.json().get("items", []), query))
            except (MapProviderError, ValueError):
                errors += 1
        if errors and not output:
            raise MapProviderError("All HERE search methods failed")
        return output

    def _map_items(self, items: list[dict[str, Any]], query: str) -> list[LocationCandidate]:
        output: list[LocationCandidate] = []
        for item in items:
            accesses = item.get("access") or [{}]
            position = item.get("position") or accesses[0]
            if "lat" not in position or "lng" not in position:
                continue
            address = item.get("address", {})
            contacts = item.get("contacts") or []
            phones = contacts[0].get("phone", []) if contacts else []
            identifier = item.get("id")
            categories = item.get("categories") or []
            output.append(
                LocationCandidate(
                    provider=self.name,
                    provider_place_id=identifier,
                    provider_place_ids={self.name: identifier} if identifier else {},
                    name=item.get("title"),
                    address=address.get("label"),
                    phone=normalize_phone(phones[0].get("value")) if phones else None,
                    category=", ".join(
                        str(value)
                        for category in categories
                        for value in [category.get("name") or category.get("id")]
                        if value
                    )
                    or None,
                    latitude=position["lat"],
                    longitude=position["lng"],
                    house_number=address.get("houseNumber"),
                    road=address.get("street"),
                    subdistrict=address.get("subdistrict"),
                    district=address.get("district") or address.get("city"),
                    province=address.get("state"),
                    postal_code=address.get("postalCode"),
                    source_query=query,
                    sources=[self.name],
                    raw=item,
                )
            )
        return output

    async def close(self) -> None:
        await self.client.aclose()

import asyncio
from typing import Any
from urllib.parse import quote

import httpx

from app.providers.base import MapProvider, MapProviderError
from app.schemas import LocationCandidate
from app.utils import normalize_phone


class GoogleMapsProvider(MapProvider):
    name = "google"
    places_url = "https://places.googleapis.com/v1/places:searchText"
    geocode_url = "https://maps.googleapis.com/maps/api/geocode/json"
    place_details_url = "https://places.googleapis.com/v1/places"

    def __init__(
        self,
        api_key: str,
        timeout: float = 4.0,
        max_retries: int = 1,
        result_limit: int = 20,
        max_pages: int = 2,
    ) -> None:
        self.api_key = api_key
        self.max_retries = max_retries
        self.result_limit = result_limit
        self.max_pages = max_pages
        self.client = httpx.AsyncClient(timeout=timeout)
        self._details_cache: dict[str, dict[str, Any]] = {}

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = await self.client.request(method, url, **kwargs)
                response.raise_for_status()
                return response
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                error = exc
                retryable = (
                    not isinstance(exc, httpx.HTTPStatusError)
                    or exc.response.status_code == 429
                    or exc.response.status_code >= 500
                )
                if attempt >= self.max_retries or not retryable:
                    break
                await asyncio.sleep(0.1 * (2**attempt))
        raise MapProviderError(f"Google Maps request failed: {type(error).__name__}") from error

    @staticmethod
    def _components(parts: list[dict[str, Any]]) -> dict[str, str]:
        result: dict[str, str] = {}
        mapping = {
            "street_number": "house_number",
            "route": "road",
            "sublocality_level_2": "subdistrict",
            "sublocality_level_1": "district",
            "administrative_area_level_2": "district",
            "administrative_area_level_1": "province",
            "postal_code": "postal_code",
        }
        for part in parts:
            for part_type in part.get("types", []):
                key = mapping.get(part_type)
                if key and key not in result:
                    result[key] = part.get("longText") or part.get("long_name") or ""
        return result

    async def search(self, query: str) -> list[LocationCandidate]:
        results = await asyncio.gather(
            self._places(query), self._geocode(query), return_exceptions=True
        )
        candidates: list[LocationCandidate] = []
        errors: list[BaseException] = []
        for result in results:
            if isinstance(result, BaseException):
                errors.append(result)
            else:
                candidates.extend(result)
        if not candidates and errors:
            message = (
                "All Google search methods failed"
                if len(errors) == len(results)
                else "Google search incomplete without candidates"
            )
            raise MapProviderError(message) from errors[0]
        return candidates

    async def _places(self, query: str) -> list[LocationCandidate]:
        headers = {
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": (
                "places.id,places.displayName,places.formattedAddress,places.location,"
                "places.addressComponents,places.nationalPhoneNumber,places.primaryType"
                ",nextPageToken"
            ),
        }
        body: dict[str, Any] = {
            "textQuery": query,
            "languageCode": "th",
            "regionCode": "TH",
            "pageSize": self.result_limit,
        }
        output: list[LocationCandidate] = []
        for _ in range(self.max_pages):
            try:
                response = await self._request("POST", self.places_url, headers=headers, json=body)
            except MapProviderError:
                if output:
                    break
                raise
            payload = response.json()
            output.extend(
                self._from_place(item, query)
                for item in payload.get("places", [])
                if item.get("location")
            )
            token = payload.get("nextPageToken")
            if not token:
                break
            body["pageToken"] = token
        return output

    def _from_place(self, item: dict[str, Any], query: str) -> LocationCandidate:
        components = self._components(item.get("addressComponents", []))
        return LocationCandidate(
            provider=self.name,
            provider_place_id=item.get("id"),
            name=item.get("displayName", {}).get("text"),
            address=item.get("formattedAddress"),
            phone=normalize_phone(item.get("nationalPhoneNumber")),
            category=item.get("primaryType"),
            latitude=item["location"]["latitude"],
            longitude=item["location"]["longitude"],
            source_query=query,
            sources=[self.name],
            provider_place_ids={self.name: item["id"]} if item.get("id") else {},
            raw=item,
            **components,
        )

    async def enrich(self, candidates: list[LocationCandidate]) -> list[LocationCandidate]:
        await asyncio.gather(
            *(self._enrich_candidate(candidate) for candidate in candidates),
            return_exceptions=True,
        )
        return candidates

    async def _enrich_candidate(self, candidate: LocationCandidate) -> None:
        place_id = candidate.provider_place_ids.get(self.name)
        if not place_id and candidate.provider == self.name:
            place_id = candidate.provider_place_id
        if not place_id:
            return
        details = self._details_cache.get(place_id)
        if details is None:
            response = await self._request(
                "GET",
                f"{self.place_details_url}/{quote(place_id, safe='')}",
                headers={
                    "X-Goog-Api-Key": self.api_key,
                    "X-Goog-FieldMask": (
                        "id,displayName,formattedAddress,location,addressComponents,"
                        "nationalPhoneNumber,internationalPhoneNumber,primaryType,types,"
                        "businessStatus,websiteUri,googleMapsUri"
                    ),
                },
                params={"languageCode": "th", "regionCode": "TH"},
            )
            details = response.json()
            if len(self._details_cache) >= 500:
                self._details_cache.pop(next(iter(self._details_cache)))
            self._details_cache[place_id] = details
        components = self._components(details.get("addressComponents", []))
        candidate.name = details.get("displayName", {}).get("text") or candidate.name
        candidate.address = details.get("formattedAddress") or candidate.address
        candidate.phone = (
            normalize_phone(
                details.get("nationalPhoneNumber") or details.get("internationalPhoneNumber")
            )
            or candidate.phone
        )
        candidate.category = details.get("primaryType") or candidate.category
        candidate.business_status = details.get("businessStatus") or candidate.business_status
        candidate.website = details.get("websiteUri") or candidate.website
        for field, value in components.items():
            if value:
                setattr(candidate, field, value)
        candidate.raw = {**(candidate.raw or {}), "placeDetails": details}

    async def _geocode(self, query: str) -> list[LocationCandidate]:
        response = await self._request(
            "GET",
            self.geocode_url,
            params={
                "address": query,
                "key": self.api_key,
                "language": "th",
                "region": "th",
                "components": "country:TH",
            },
        )
        payload = response.json()
        status = payload.get("status")
        if status not in {"OK", "ZERO_RESULTS"}:
            raise MapProviderError(f"Google Geocoding returned {status}")
        output: list[LocationCandidate] = []
        for item in payload.get("results", [])[: self.result_limit]:
            location = item.get("geometry", {}).get("location")
            if location:
                output.append(
                    LocationCandidate(
                        provider=self.name,
                        provider_place_id=item.get("place_id"),
                        address=item.get("formatted_address"),
                        latitude=location["lat"],
                        longitude=location["lng"],
                        source_query=query,
                        sources=[self.name],
                        provider_place_ids=(
                            {self.name: item["place_id"]} if item.get("place_id") else {}
                        ),
                        raw=item,
                        category=", ".join(item.get("types", [])) or None,
                        **self._components(item.get("address_components", [])),
                    )
                )
        return output

    async def close(self) -> None:
        await self.client.aclose()

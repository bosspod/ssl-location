from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from app.schemas import (
    DBDResearchStatus,
    ParsedLocation,
    ResearchEvidence,
)
from app.utils import compact_text, place_name_similarity


def business_name_queries(company_name: str, aliases: list[str]) -> list[str]:
    legal_terms = (
        r"บริษัท|ห้างหุ้นส่วนจำกัด|หจก\.?|จำกัด|มหาชน|company|co\.?|ltd\.?|limited"
    )
    output: list[str] = []
    for value in [company_name, *aliases]:
        stripped = " ".join(re.sub(legal_terms, " ", value, flags=re.IGNORECASE).split())
        key = compact_text(stripped)
        seen = {compact_text(item) for item in output}
        if len(key) >= 3 and key not in seen:
            output.append(stripped)
        if len(output) >= 3:
            return output
    return output


@dataclass
class DBDRecord:
    registration_number: str
    company_name: str
    company_name_en: str | None
    registered_address: str
    branch_name: str | None
    status: str | None
    source_url: str


@dataclass
class BusinessRegistryResult:
    parsed: ParsedLocation
    dbd: DBDResearchStatus
    map_queries: list[str] = field(default_factory=list)
    evidence: list[ResearchEvidence] = field(default_factory=list)


class DBDRegistryClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
        name_api_base_url: str = "https://api.egov.go.th",
        consumer_key: str | None = None,
        consumer_secret: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.name_api_base_url = name_api_base_url.rstrip("/")
        self.consumer_key = consumer_key
        self.consumer_secret = consumer_secret
        self.agent_id = agent_id
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=timeout_seconds)

    @property
    def name_search_configured(self) -> bool:
        return bool(self.consumer_key and self.consumer_secret and self.agent_id)

    async def lookup(self, registration_number: str) -> DBDRecord | None:
        if not re.fullmatch(r"\d{13}", registration_number):
            return None
        records = await self._search_query(registration_number)
        return next(
            (
                record
                for record in records
                if record.registration_number == registration_number
            ),
            None,
        )

    async def search(self, company_name: str, aliases: list[str]) -> list[DBDRecord]:
        if not self.name_search_configured:
            return []
        queries = business_name_queries(company_name, aliases)
        if not queries:
            return []
        token = await self._name_search_token()
        results = await asyncio.gather(
            *(self._search_name(query, token) for query in queries),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors and len(errors) == len(results):
            raise errors[0]
        identifiers = list(
            dict.fromkeys(
                identifier
                for result in results
                if isinstance(result, list)
                for identifier in result
                if re.fullmatch(r"\d{13}", identifier)
            )
        )[:20]
        lookups = await asyncio.gather(
            *(self.lookup(identifier) for identifier in identifiers),
            return_exceptions=True,
        )
        records = [result for result in lookups if isinstance(result, DBDRecord)]
        return self._deduplicate(records)

    async def _name_search_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        async with self._token_lock:
            if self._token and time.monotonic() < self._token_expires_at:
                return self._token
            response = await self.client.get(
                f"{self.name_api_base_url}/ws/auth/validate",
                params={
                    "ConsumerSecret": self.consumer_secret,
                    "AgentID": self.agent_id,
                },
                headers={"Consumer-Key": str(self.consumer_key)},
            )
            response.raise_for_status()
            token = str(response.json().get("Result") or "").strip()
            if not token:
                raise ValueError("DGA authentication response did not contain a token")
            self._token = token
            self._token_expires_at = time.monotonic() + 50 * 60
            return token

    async def _search_name(self, query: str, token: str) -> list[str]:
        response = await self.client.get(
            f"{self.name_api_base_url}/ws/dbd/juristic/v4/profile/infobyname",
            params={"Name": query},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Consumer-Key": str(self.consumer_key),
                "Token": token,
            },
        )
        response.raise_for_status()
        payload = response.json()
        return [
            str(item.get("JuristicID") or "").strip()
            for item in (payload.get("ResultList") or [])
            if isinstance(item, dict)
        ]

    async def _search_query(self, query: str) -> list[DBDRecord]:
        url = f"{self.base_url}/juristic_person/{quote(query, safe='')}"
        response = await self.client.get(url)
        response.raise_for_status()
        payload = response.json()
        if payload.get("status", {}).get("code") != "1000":
            return []
        data = payload.get("data") or []
        return [
            record
            for item in data
            if isinstance(item, dict)
            and (record := self._record(item, url)) is not None
        ]

    @classmethod
    def _record(cls, value: dict[str, Any], url: str) -> DBDRecord | None:
        person = value.get("cd:OrganizationJuristicPerson") or {}
        address = person.get("cd:OrganizationJuristicAddress", {}).get("cr:AddressType") or {}
        company_name = str(person.get("cd:OrganizationJuristicNameTH") or "").strip()
        registered_address = cls._address(address)
        if not company_name or not registered_address:
            return None
        return DBDRecord(
            registration_number=str(person.get("cd:OrganizationJuristicID") or ""),
            company_name=company_name,
            company_name_en=cls._text(person.get("cd:OrganizationJuristicNameEN")),
            registered_address=registered_address,
            branch_name=cls._text(person.get("cd:OrganizationJuristicBranchName")),
            status=cls._text(person.get("cd:OrganizationJuristicStatus")),
            source_url=url,
        )

    @staticmethod
    def _deduplicate(records: list[DBDRecord]) -> list[DBDRecord]:
        output: list[DBDRecord] = []
        seen: set[str] = set()
        for record in records:
            key = record.registration_number or compact_text(record.company_name)
            if key and key not in seen:
                seen.add(key)
                output.append(record)
        return output

    @staticmethod
    def _identity_similarity(expected: str, actual: str) -> float:
        expected_key = compact_text(expected)
        actual_key = compact_text(actual)
        if len(expected_key) >= 3 and expected_key in actual_key:
            return 1.0
        return place_name_similarity(expected, actual)

    @classmethod
    def _address(cls, value: dict[str, Any]) -> str:
        parts = [
            cls._text(value.get("cd:Address")),
            cls._admin(value.get("cd:CitySubDivision"), "cr:CitySubDivisionTextTH"),
            cls._admin(value.get("cd:City"), "cr:CityTextTH"),
            cls._admin(value.get("cd:CountrySubDivision"), "cr:CountrySubDivisionTextTH"),
        ]
        return " ".join(dict.fromkeys(part for part in parts if part))

    @classmethod
    def _admin(cls, value: Any, key: str) -> str | None:
        return cls._text(value.get(key)) if isinstance(value, dict) else None

    @staticmethod
    def _text(value: Any) -> str | None:
        text = str(value if value is not None else "").strip()
        return text if text and text != "-" else None

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()


class BusinessRegistryService:
    def __init__(self, dbd: DBDRegistryClient | None) -> None:
        self.dbd = dbd

    async def research(self, parsed: ParsedLocation) -> BusinessRegistryResult:
        company_name = parsed.company_name or self._legal_place_name(parsed.place_name)
        dbd_records: list[DBDRecord] = []
        dbd_attempted = False
        dbd_successful_operations = 0

        if self.dbd and company_name and self.dbd.name_search_configured:
            dbd_attempted = True
            try:
                dbd_records.extend(await self.dbd.search(company_name, parsed.aliases))
                dbd_successful_operations += 1
            except (httpx.HTTPError, ValueError):
                pass

        identifiers = list(
            dict.fromkeys(
                identifier
                for identifier in [parsed.registration_number]
                if identifier and re.fullmatch(r"\d{13}", identifier)
            )
        )
        if self.dbd and identifiers:
            dbd_attempted = True
            known_identifiers = {record.registration_number for record in dbd_records}
            lookup_identifiers = [
                identifier for identifier in identifiers if identifier not in known_identifiers
            ]
            results = await asyncio.gather(
                *(self.dbd.lookup(identifier) for identifier in lookup_identifiers),
                return_exceptions=True,
            )
            dbd_successful_operations += sum(
                not isinstance(result, BaseException) for result in results
            )
            dbd_records.extend(
                result for result in results if isinstance(result, DBDRecord)
            )
        if self.dbd:
            dbd_records = DBDRegistryClient._deduplicate(dbd_records)
        record = self._best_dbd_record(company_name, parsed.aliases, dbd_records)
        updated = parsed
        evidence: list[ResearchEvidence] = []
        map_queries: list[str] = []
        if record:
            aliases = list(
                dict.fromkeys(
                    [
                        *parsed.aliases,
                        *(value for value in [record.company_name_en] if value),
                    ]
                )
            )
            updated = parsed.model_copy(
                update={
                    "company_name": record.company_name,
                    "aliases": aliases,
                    "registration_number": record.registration_number,
                    "registered_address": record.registered_address,
                }
            )
            evidence.append(ResearchEvidence(title="DBD Open API", url=record.source_url))
            map_queries.append(f"{record.company_name} {record.registered_address}")
            dbd_status = DBDResearchStatus(
                status="searched",
                reason="ตรวจชื่อ เลขทะเบียน และที่อยู่จดทะเบียนจาก DBD Open API แล้ว",
                queries=[*business_name_queries(company_name or "", parsed.aliases), *identifiers],
                company_name=record.company_name,
                registration_number=record.registration_number,
                registered_address=record.registered_address,
                evidence=list(evidence),
            )
        elif dbd_attempted and dbd_successful_operations == 0:
            dbd_status = DBDResearchStatus(
                status="unavailable",
                reason="DBD Open API ใช้งานไม่ได้ชั่วคราว",
                queries=[*business_name_queries(company_name or "", parsed.aliases), *identifiers],
            )
        elif dbd_attempted:
            dbd_status = DBDResearchStatus(
                status="not_found",
                reason="ค้น DBD ด้วยชื่อหรือเลขทะเบียนแล้ว แต่ไม่พบระเบียนที่ชื่อสอดคล้องกัน",
                queries=[*business_name_queries(company_name or "", parsed.aliases), *identifiers],
            )
        elif self.dbd and company_name and not self.dbd.name_search_configured:
            dbd_status = DBDResearchStatus(
                status="unavailable",
                reason=(
                    "DBD Open API สาธารณะตรวจด้วยเลขทะเบียน 13 หลักได้ แต่การค้นด้วยชื่อ"
                    "ผ่าน DGA/GDX ต้องตั้งค่า DBD_CONSUMER_KEY, DBD_CONSUMER_SECRET "
                    "และ DBD_AGENT_ID"
                ),
                queries=business_name_queries(company_name, parsed.aliases),
            )
        else:
            dbd_status = DBDResearchStatus()

        return BusinessRegistryResult(
            parsed=updated,
            dbd=dbd_status,
            map_queries=list(dict.fromkeys(map_queries)),
            evidence=self._unique_evidence(evidence),
        )

    @staticmethod
    def _legal_place_name(place_name: str | None) -> str | None:
        if place_name and re.search(r"บริษัท|ห้างหุ้นส่วน|\b(?:co|company|ltd)\b", place_name, re.I):
            return place_name
        return None

    @staticmethod
    def _best_dbd_record(
        company_name: str | None,
        aliases: list[str],
        records: list[DBDRecord],
    ) -> DBDRecord | None:
        if not records:
            return None
        if not company_name:
            return records[0] if len(records) == 1 else None
        identities = [company_name, *aliases]
        scored = [
            (
                max(
                    DBDRegistryClient._identity_similarity(identity, record.company_name)
                    for identity in identities
                    if identity
                ),
                record,
            )
            for record in records
        ]
        score, record = max(scored, key=lambda item: item[0])
        return record if score >= 0.65 else None

    @staticmethod
    def _unique_evidence(items: list[ResearchEvidence]) -> list[ResearchEvidence]:
        output: list[ResearchEvidence] = []
        seen: set[str] = set()
        for item in items:
            if item.url not in seen:
                seen.add(item.url)
                output.append(item)
        return output

    async def close(self) -> None:
        if self.dbd:
            await self.dbd.close()

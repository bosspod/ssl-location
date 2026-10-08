from collections.abc import AsyncIterator
from typing import Any

from pymongo import ASCENDING, AsyncMongoClient, IndexModel
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import OperationFailure

from app.config import get_settings

MongoDatabase = AsyncDatabase[dict[str, Any]]

settings = get_settings()
client: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(settings.database_url)
database: MongoDatabase = client[settings.mongodb_database]


async def get_db() -> AsyncIterator[MongoDatabase]:
    yield database


async def ensure_indexes(db: MongoDatabase) -> None:
    indexes = await db.locations.index_information()
    legacy = indexes.get("provider_place_id_1")
    if legacy and legacy.get("unique"):
        try:
            await db.locations.drop_index("provider_place_id_1")
        except OperationFailure as exc:
            if exc.code != 27:
                raise
    for provider in ("google", "here", "longdo", "openstreetmap"):
        name = f"provider_place_ids.{provider}_1"
        existing = indexes.get(name)
        if existing and not existing.get("unique"):
            try:
                await db.locations.drop_index(name)
            except OperationFailure as exc:
                if exc.code != 27:
                    raise
    await db.locations.create_indexes(
        [
            IndexModel([("provider_place_id", ASCENDING)], sparse=True),
            IndexModel([("phone", ASCENDING)]),
            IndexModel([("postal_code", ASCENDING)]),
            IndexModel([("province", ASCENDING)]),
            IndexModel([("normalized_address", ASCENDING)]),
            IndexModel([("aliases.normalized_alias", ASCENDING)]),
            IndexModel([("search_terms", ASCENDING)]),
            IndexModel([("provider_place_ids.google", ASCENDING)], unique=True, sparse=True),
            IndexModel([("provider_place_ids.here", ASCENDING)], unique=True, sparse=True),
            IndexModel([("provider_place_ids.longdo", ASCENDING)], unique=True, sparse=True),
            IndexModel([("provider_place_ids.openstreetmap", ASCENDING)], unique=True, sparse=True),
            IndexModel([("location", "2dsphere")]),
        ]
    )


async def ping(db: MongoDatabase) -> None:
    await db.command("ping")

import asyncio
import time
from abc import ABC, abstractmethod


class Cache[T](ABC):
    @abstractmethod
    async def get(self, key: str) -> T | None: ...

    @abstractmethod
    async def set(self, key: str, value: T, ttl: int) -> None: ...


class MemoryTTLCache[T](Cache[T]):
    def __init__(self, maximum_entries: int = 5000) -> None:
        self._items: dict[str, tuple[float, T]] = {}
        self._maximum_entries = maximum_entries
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> T | None:
        async with self._lock:
            item = self._items.get(key)
            if not item:
                return None
            expires, value = item
            if expires <= time.monotonic():
                self._items.pop(key, None)
                return None
            return value

    async def set(self, key: str, value: T, ttl: int) -> None:
        async with self._lock:
            now = time.monotonic()
            expired = [item_key for item_key, item in self._items.items() if item[0] <= now]
            for item_key in expired:
                self._items.pop(item_key, None)
            if key not in self._items and len(self._items) >= self._maximum_entries:
                oldest = min(self._items, key=lambda item_key: self._items[item_key][0])
                self._items.pop(oldest, None)
            self._items[key] = (now + ttl, value)

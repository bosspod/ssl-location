import asyncio
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

from app.config import Settings


class InMemoryRateLimiter:
    """Single-process limiter; use an API gateway limiter when horizontally scaled."""

    def __init__(self, settings: Settings) -> None:
        self.limit = settings.rate_limit_requests
        self.window = settings.rate_limit_window_seconds
        self.maximum_clients = settings.rate_limit_max_clients
        self.requests: dict[str, deque[float]] = defaultdict(deque)
        self.lock = asyncio.Lock()

    async def check(self, request: Request) -> None:
        key = request.client.host if request.client else "unknown"
        now = time.monotonic()
        async with self.lock:
            if key not in self.requests and len(self.requests) >= self.maximum_clients:
                self.requests.pop(next(iter(self.requests)), None)
            timestamps = self.requests[key]
            while timestamps and timestamps[0] <= now - self.window:
                timestamps.popleft()
            if len(timestamps) >= self.limit:
                raise HTTPException(status_code=429, detail="Too many requests")
            timestamps.append(now)

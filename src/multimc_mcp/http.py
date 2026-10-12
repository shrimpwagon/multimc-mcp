"""Shared async HTTP client with a small in-memory cache and polite retries."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from . import __version__

USER_AGENT = f"shrimpwagon/multimc-mcp/{__version__} (+https://github.com/shrimpwagon/multimc-mcp)"


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} from {url}{': ' + body[:200] if body else ''}")
        self.status = status


class Http:
    def __init__(self) -> None:
        self.client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=httpx.Timeout(30, connect=10),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=12),
        )
        self._cache: dict[str, tuple[float, Any]] = {}

    async def request_json(self, method: str, url: str, *, params: dict | None = None,
                           body: Any = None, ttl: float = 0, headers: dict | None = None) -> Any:
        key = f"{method} {url} {json.dumps(params, sort_keys=True)} {json.dumps(body, sort_keys=True)}"
        if ttl:
            hit = self._cache.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
        for attempt in range(4):
            try:
                r = await self.client.request(method, url, params=params, json=body, headers=headers)
            except httpx.TransportError:
                if attempt == 3:
                    raise
                await asyncio.sleep(1 + attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == 3:
                    raise HttpError(r.status_code, url, r.text)
                wait = float(r.headers.get("X-Ratelimit-Reset") or r.headers.get("Retry-After") or (1 + attempt))
                await asyncio.sleep(min(wait, 30))
                continue
            if r.status_code >= 400:
                raise HttpError(r.status_code, url, r.text)
            data = r.json()
            if ttl:
                self._cache[key] = (time.monotonic() + ttl, data)
            return data
        raise AssertionError("unreachable")

    async def get_json(self, url: str, *, params: dict | None = None, ttl: float = 300,
                       headers: dict | None = None) -> Any:
        return await self.request_json("GET", url, params=params, ttl=ttl, headers=headers)

    async def post_json(self, url: str, body: Any, *, headers: dict | None = None) -> Any:
        return await self.request_json("POST", url, body=body, headers=headers)

    async def aclose(self) -> None:
        await self.client.aclose()

"""Couche réseau : transport abstrait, limiteur de débit, quota quotidien, santé des fournisseurs, client API."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from ..core.clock import Clock

log = logging.getLogger("radar.net")


@dataclass
class HttpResponse:
    status: int
    text: str
    headers: dict = field(default_factory=dict)

    def json(self) -> Any:
        return json.loads(self.text)


class Transport(Protocol):
    async def request(self, method: str, url: str, *, params: dict | None = None, headers: dict | None = None,
                      json_body: Any = None, timeout: float = 10.0) -> HttpResponse: ...


class UrllibTransport:
    """Transport 100 % bibliothèque standard (aucun paquet à installer). Les appels bloquants partent dans un fil."""

    async def request(self, method, url, *, params=None, headers=None, json_body=None, timeout=10.0) -> HttpResponse:
        import gzip
        import urllib.error
        import urllib.parse
        import urllib.request

        def run() -> HttpResponse:
            full = url + (("&" if "?" in url else "?") + urllib.parse.urlencode(params) if params else "")
            data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
            hdrs = dict(headers or {})
            if data is not None:
                hdrs.setdefault("Content-Type", "application/json")
            req = urllib.request.Request(full, data=data, headers=hdrs, method=method)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    status, body, h = r.status, r.read(), r.headers
            except urllib.error.HTTPError as e:      # 4xx/5xx : on rend la réponse, le client décide
                status, body, h = e.code, e.read(), e.headers
            if h.get("Content-Encoding", "").lower() == "gzip":
                body = gzip.decompress(body)
            return HttpResponse(status, body.decode(h.get_content_charset() or "utf-8", "replace"),
                                {k.lower(): v for k, v in h.items()})

        return await asyncio.to_thread(run)

    async def aclose(self) -> None:
        return None


class QuotaExceeded(Exception):
    pass


class ApiError(Exception):
    def __init__(self, status: int, body: str = ""):
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status, self.body = status, body


class RateLimiter:
    def __init__(self, per_second: float):
        self._interval = 1.0 / per_second
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._next = max(now, self._next) + self._interval


class DailyQuota:
    def __init__(self, limit: int, clock: Clock):
        self.limit, self.used, self._clock, self._day = limit, 0, clock, clock.now().date()

    def consume(self) -> None:
        today = self._clock.now().date()
        if today != self._day:
            self._day, self.used = today, 0
        if self.used >= self.limit:
            raise QuotaExceeded(f"quota quotidien atteint ({self.limit})")
        self.used += 1


@dataclass
class Health:
    provider: str
    capability: str
    mode: str                       # REALTIME | DELAYED | POLLING | HISTORICAL | UNAVAILABLE
    note: str = ""
    last_ok_at: datetime | None = None
    last_data_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error: str | None = None
    last_latency_ms: float | None = None
    ok_count: int = 0
    err_count: int = 0
    quota_limit: int | None = None
    quota_used: int | None = None


class HealthRegistry:
    def __init__(self, clock: Clock):
        self._clock = clock
        self._items: dict[tuple[str, str], Health] = {}

    def declare(self, provider: str, capability: str, mode: str, note: str = "") -> Health:
        return self._items.setdefault((provider, capability), Health(provider, capability, mode, note))

    def _get(self, p: str, c: str) -> Health:
        return self._items.setdefault((p, c), Health(p, c, "POLLING"))

    def ok(self, p: str, c: str, latency_ms: float | None = None, data: bool = False) -> None:
        h, now = self._get(p, c), self._clock.now()
        h.last_ok_at, h.ok_count = now, h.ok_count + 1
        if latency_ms is not None:
            h.last_latency_ms = latency_ms
        if data:
            h.last_data_at = now

    def error(self, p: str, c: str, message: str) -> None:
        h = self._get(p, c)
        h.last_error_at, h.last_error, h.err_count = self._clock.now(), message[:300], h.err_count + 1

    def set_quota(self, p: str, c: str, limit: int, used: int) -> None:
        h = self._get(p, c)
        h.quota_limit, h.quota_used = limit, used

    def get(self, p: str, c: str) -> Health | None:
        return self._items.get((p, c))

    def snapshot(self) -> list[dict]:
        iso = lambda d: d.isoformat() if d else None
        return [{
            "provider": h.provider, "capability": h.capability, "mode": h.mode, "note": h.note,
            "last_ok_at": iso(h.last_ok_at), "last_data_at": iso(h.last_data_at), "last_error_at": iso(h.last_error_at),
            "last_error": h.last_error, "last_latency_ms": h.last_latency_ms, "ok_count": h.ok_count,
            "err_count": h.err_count, "quota_limit": h.quota_limit, "quota_used": h.quota_used,
        } for h in self._items.values()]


class ApiClient:
    """Client HTTP avec limiteur, quota, nouvelles tentatives (429/5xx) et suivi de santé.
    Ne journalise jamais les paramètres d'URL (ils peuvent contenir des clés d'API)."""

    def __init__(self, name: str, capability: str, transport: Transport, *, headers: dict | None = None,
                 limiter: RateLimiter | None = None, quota: DailyQuota | None = None, retries: int = 2,
                 timeout: float = 10.0, backoff: float = 0.5, health: HealthRegistry | None = None, sleep=asyncio.sleep):
        self.name, self.capability, self.transport = name, capability, transport
        self.headers, self.limiter, self.quota = headers or {}, limiter, quota
        self.retries, self.timeout, self.backoff, self.health, self._sleep = retries, timeout, backoff, health, sleep

    def _err(self, msg: str) -> None:
        log.warning("%s/%s: %s", self.name, self.capability, msg)
        if self.health:
            self.health.error(self.name, self.capability, msg)

    async def request(self, method: str, url: str, *, params: dict | None = None, headers: dict | None = None,
                      json_body: Any = None) -> HttpResponse:
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            if self.quota:
                self.quota.consume()
                if self.health:
                    self.health.set_quota(self.name, self.capability, self.quota.limit, self.quota.used)
            if self.limiter:
                await self.limiter.acquire()
            t0 = time.perf_counter()
            try:
                resp = await self.transport.request(method, url, params=params, headers={**self.headers, **(headers or {})},
                                                    json_body=json_body, timeout=self.timeout)
            except Exception as ex:   # noqa: BLE001 — erreurs réseau
                last = ex
                self._err(f"réseau: {type(ex).__name__}")
                await self._sleep(self.backoff * 2 ** attempt)
                continue
            ms = (time.perf_counter() - t0) * 1000
            if resp.status in (429, 500, 502, 503, 504) and attempt < self.retries:
                self._err(f"HTTP {resp.status}")
                try:
                    wait = min(float(resp.headers.get("retry-after", "")), 30.0)
                except ValueError:
                    wait = self.backoff * 2 ** attempt
                await self._sleep(wait)
                continue
            if resp.status >= 400:
                self._err(f"HTTP {resp.status}")
                raise ApiError(resp.status, resp.text)
            if self.health:
                self.health.ok(self.name, self.capability, latency_ms=ms)
            return resp
        raise ApiError(0, f"échec réseau: {last!r}")

    async def get_text(self, url: str, params: dict | None = None, headers: dict | None = None) -> HttpResponse:
        return await self.request("GET", url, params=params, headers=headers)

    async def get_json(self, url: str, params: dict | None = None, headers: dict | None = None) -> Any:
        return (await self.request("GET", url, params=params, headers=headers)).json()

    async def post_json(self, url: str, body: Any, headers: dict | None = None) -> HttpResponse:
        return await self.request("POST", url, json_body=body, headers=headers)

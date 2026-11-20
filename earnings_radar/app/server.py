"""Serveur web minimal 100 % bibliothèque standard : API JSON, flux temps réel (SSE) et fichiers du site.
Aucun paquet à installer. Écoute par défaut sur 127.0.0.1 (ce PC seulement)."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import parse_qs, unquote, urlsplit

from .. import __version__
from ..core.marketcal import et_date
from .report import render_report

log = logging.getLogger("radar.web")
MAX_BODY = 65536
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".svg": "image/svg+xml", ".json": "application/json; charset=utf-8", ".png": "image/png", ".ico": "image/x-icon",
        ".txt": "text/plain; charset=utf-8", ".webmanifest": "application/manifest+json"}
REASONS = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 413: "Payload Too Large",
           500: "Internal Server Error"}


@dataclass
class Request:
    method: str
    target: str
    headers: dict
    body: bytes
    too_large: bool = False


def _j(status: int, obj) -> tuple[int, str, bytes]:
    return status, MIME[".json"], json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")


def _int(v, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


class WebServer:
    def __init__(self, rt, host: str = "127.0.0.1", port: int = 8765, static_dir: str = "web"):
        self.rt, self.host, self.port, self.static_dir = rt, host, port, static_dir
        self.token = rt.settings.access_token
        self._server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._client, self.host, self.port, limit=MAX_BODY + 16384)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server:
            self._server.close()
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server:
            try:
                await asyncio.wait_for(self._server.wait_closed(), 3)
            except asyncio.TimeoutError:
                pass

    # ---------- protocole ----------
    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            req = await asyncio.wait_for(self._read(reader), 15)
            await self._dispatch(req, reader, writer)
        except (asyncio.TimeoutError, ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:   # noqa: BLE001
            log.exception("erreur serveur")
            try:
                await self._send(writer, *_j(500, {"error": "erreur interne"}))
            except Exception:   # noqa: BLE001
                pass
        finally:
            self._tasks.discard(task)
            writer.close()

    async def _read(self, reader: asyncio.StreamReader) -> Request:
        head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1").split("\r\n")
        method, target, _ = head[0].split(" ", 2)
        headers = {}
        for ln in head[1:]:
            if ":" in ln:
                k, _, v = ln.partition(":")
                headers[k.strip().lower()] = v.strip()
        n = int(headers.get("content-length", "0") or 0)
        if n > MAX_BODY:
            return Request(method, target, headers, b"", too_large=True)
        return Request(method, target, headers, await reader.readexactly(n) if n else b"")

    async def _send(self, writer: asyncio.StreamWriter, status: int, ctype: str, body: bytes) -> None:
        head = (f"HTTP/1.1 {status} {REASONS.get(status, 'OK')}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
                "Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nConnection: close\r\n\r\n")
        writer.write(head.encode("latin-1") + body)
        await writer.drain()

    def _authorized(self, req: Request, q: dict) -> bool:
        if not self.token:
            return True
        given = req.headers.get("authorization", "").removeprefix("Bearer ").strip() or q.get("token", "")
        return bool(given) and hmac.compare_digest(given, self.token)

    async def _dispatch(self, req: Request, reader, writer) -> None:
        if req.too_large:
            return await self._send(writer, *_j(413, {"error": "requête trop grande"}))
        split = urlsplit(req.target)
        path, q = unquote(split.path), {k: v[0] for k, v in parse_qs(split.query).items()}
        if path.startswith("/api/"):
            if path != "/api/health" and not self._authorized(req, q):
                return await self._send(writer, *_j(401, {"error": "jeton d'accès requis"}))
            if path == "/api/stream" and req.method == "GET":
                return await self._stream(reader, writer)
            return await self._send(writer, *(await self._api(req, path, q)))
        await self._send(writer, *self._static(path))

    # ---------- site ----------
    def _static(self, path: str) -> tuple[int, str, bytes]:
        if path in ("", "/"):
            path = "/index.html"
        root = os.path.realpath(self.static_dir)
        full = os.path.realpath(os.path.join(root, path.lstrip("/\\")))
        try:
            inside = os.path.commonpath([root, full]) == root
        except ValueError:
            inside = False
        if not inside:
            return 403, MIME[".txt"], b"interdit"
        if not os.path.isfile(full):
            return 404, MIME[".txt"], "introuvable".encode()
        with open(full, "rb") as f:
            return 200, MIME.get(os.path.splitext(full)[1].lower(), "application/octet-stream"), f.read()

    # ---------- API ----------
    async def _api(self, req: Request, path: str, q: dict) -> tuple[int, str, bytes]:
        svc, rt, m = self.rt.service, self.rt, req.method
        try:
            if m == "GET":
                today = et_date(rt.clock.now())
                if path == "/api/health":
                    return _j(200, {"ok": True, "version": __version__, "auth_required": bool(self.token)})
                if path == "/api/status":
                    return _j(200, await svc.status())
                if path == "/api/earnings/today":
                    return _j(200, await svc.earnings_today())
                if path == "/api/earnings":
                    d_from = date.fromisoformat(q["from"]) if q.get("from") else today
                    d_to = date.fromisoformat(q["to"]) if q.get("to") else today + timedelta(days=rt.settings.calendar_lookahead_days)
                    return _j(200, await svc.earnings(d_from, d_to, q.get("status")))
                if path == "/api/events":
                    return _j(200, await svc.events(_int(q.get("limit"), 100, 1, 500)))
                if path == "/api/revisions":
                    return _j(200, await svc.revisions(_int(q.get("limit"), 50, 1, 500)))
                if path == "/api/analytics":
                    return _j(200, await svc.analytics())
                if path == "/api/follows":
                    return _j(200, await svc.follows())
                if path == "/api/search":
                    return _j(200, svc.search(q.get("q", "")))
                if path == "/api/report.html":
                    html = render_report(await svc.status(), await svc.earnings_today(), await svc.earnings(today, today + timedelta(days=14)),
                                         await svc.events(30), await svc.analytics())
                    return 200, MIME[".html"], html.encode("utf-8")
            elif m == "POST":
                body = json.loads(req.body.decode("utf-8")) if req.body else {}
                if path == "/api/follows":
                    return _j(200, await svc.follow(str(body.get("ticker", ""))))
                if path == "/api/test-event":
                    if not rt.settings.enable_test_injection:
                        return _j(403, {"error": "injection de test désactivée (ENABLE_TEST_INJECTION=false)"})
                    return _j(200, await svc.inject_test_event(str(body.get("ticker") or q.get("ticker") or "TEST")))
                if path == "/api/calendar/refresh":
                    rt.calendar.wakeup.set()
                    return _j(200, {"ok": True})
            elif m == "DELETE" and path.startswith("/api/follows/"):
                return _j(200, await svc.unfollow(path.rsplit("/", 1)[1]))
        except ValueError as ex:      # inclut JSON invalide et dates invalides
            return _j(400, {"error": "requête invalide" if isinstance(ex, json.JSONDecodeError) else str(ex)})
        except LookupError as ex:
            return _j(404, {"error": str(ex)})
        return _j(404, {"error": "route inconnue"})

    # ---------- temps réel (Server-Sent Events) ----------
    async def _stream(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\n"
                     b"Connection: keep-alive\r\nX-Accel-Buffering: no\r\n\r\n: connecte\n\n")
        await writer.drain()
        sub = self.rt.bus.subscribe()
        getter, closer = asyncio.ensure_future(sub.get()), asyncio.ensure_future(reader.read(1))
        try:
            while True:
                done, _ = await asyncio.wait({getter, closer}, timeout=15, return_when=asyncio.FIRST_COMPLETED)
                if closer in done:
                    break                                    # le navigateur a fermé la connexion
                if getter in done:
                    writer.write(b"data: " + json.dumps(getter.result(), ensure_ascii=False, default=str).encode("utf-8") + b"\n\n")
                    getter = asyncio.ensure_future(sub.get())
                else:
                    writer.write(b": ping\n\n")
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            getter.cancel()
            closer.cancel()
            sub.close()

"""Assemblage de l'application : construit les composants, lance les tâches de fond, les arrête proprement."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from ..collectors.edgar import EdgarClient, EdgarMonitor, Universe
from ..config import Settings
from ..core.clock import Clock, SystemClock
from ..core.eventbus import EventBus
from ..db.core import open_database
from ..db.repo import Repo
from ..detection.pipeline import DetectionPipeline
from ..detection.watch import Reminders, Watchdog
from ..earnings.engine import CalendarEngine
from ..earnings.providers import FinnhubCalendar, FmpCalendar
from ..net.http import ApiClient, DailyQuota, HealthRegistry, RateLimiter, UrllibTransport
from ..notifications.notifiers import build_notifiers
from .service import Service

log = logging.getLogger("radar.runtime")


class Runtime:
    def __init__(self, settings: Settings, *, transport=None, clock: Clock | None = None, db=None):
        self.settings, self._transport, self.clock, self._db = settings, transport, clock or SystemClock(), db
        self.started_at: datetime = self.clock.now()
        self.stop_event = asyncio.Event()
        self.tasks: list[asyncio.Task] = []
        self.edgar: EdgarMonitor | None = None
        self.edgar_client: EdgarClient | None = None

    async def build(self) -> "Runtime":
        s = self.settings
        self.db = self._db or await open_database(s.database_url)
        self.repo, self.bus, self.health = Repo(self.db), EventBus(), HealthRegistry(self.clock)
        self.transport = self._transport or UrllibTransport()
        self._declare()
        self.universe = Universe(None, s.universe_exchanges)
        if s.sec_user_agent:
            hdr = {"User-Agent": s.sec_user_agent}
            self.edgar_client = EdgarClient(ApiClient("sec_edgar", "filings_8k", self.transport, health=self.health, headers=hdr,
                                                      limiter=RateLimiter(8)))
            self.ref_client = EdgarClient(ApiClient("sec_edgar", "reference", self.transport, health=self.health, headers=hdr,
                                                    limiter=RateLimiter(2)))
            await self.load_universe()
        providers = {}
        if s.finnhub_api_key:
            providers["finnhub"] = FinnhubCalendar(ApiClient("finnhub", "earnings_calendar", self.transport, health=self.health,
                                                             limiter=RateLimiter(1)), s.finnhub_api_key)
        if s.fmp_api_key:
            providers["fmp"] = FmpCalendar(ApiClient("fmp", "earnings_calendar", self.transport, health=self.health,
                                                     quota=DailyQuota(s.fmp_daily_quota, self.clock), limiter=RateLimiter(2)), s.fmp_api_key)
        order = [n for n in s.calendar_priority if n in providers] + [n for n in providers if n not in s.calendar_priority]
        self.calendar = CalendarEngine(self.repo, [providers[n] for n in order], self.universe, self.bus, self.clock,
                                       priority=list(s.calendar_priority), lookahead_days=s.calendar_lookahead_days, health=self.health,
                                       intervals=(s.refresh_idle_minutes, s.refresh_active_minutes, s.refresh_hot_minutes))
        self.notifiers = build_notifiers(s, self.transport, self.health)
        self.pipeline = DetectionPipeline(self.repo, self.bus, self.calendar, self.notifiers, self.clock,
                                          follows_provider=self.repo.follows_set)
        if self.edgar_client:
            self.edgar = EdgarMonitor(self.edgar_client, self.pipeline.ingest, self.universe, self.repo, self.clock, health=self.health,
                                      poll_seconds=s.edgar_poll_seconds, backfill_hours=s.edgar_backfill_hours,
                                      recovery_pages=s.edgar_recovery_pages, priority_ciks=self._priority_ciks)
        self.reminders = Reminders(self.repo, self.bus, self.notifiers, self.clock)
        self.watchdog = Watchdog(self.health, self.repo, self.bus, self.notifiers, self.clock, s.watchdog_stale_seconds)
        self.service = Service(self)
        return self

    def _priority_ciks(self) -> set[str]:
        tm = self.universe.tickers
        return {c for t in self.calendar.today_tickers if tm and (c := tm.cik_for_ticker(t))}

    def _declare(self) -> None:
        s, h = self.settings, self.health
        off = lambda ok: "POLLING" if ok else "UNAVAILABLE"
        h.declare("sec_edgar", "filings_8k", off(bool(s.sec_user_agent)),
                  "Dépôts 8-K de la SEC, vérifiés toutes les ~5 s" if s.sec_user_agent else "non configuré : identification SEC manquante")
        h.declare("sec_edgar", "reference", off(bool(s.sec_user_agent)), "Liste des sociétés cotées, 1×/jour")
        h.declare("finnhub", "earnings_calendar", off(bool(s.finnhub_api_key)),
                  "Résultats attendus (clé gratuite)" if s.finnhub_api_key else "non configuré (clé gratuite facultative)")
        h.declare("fmp", "earnings_calendar", off(bool(s.fmp_api_key)),
                  f"Résultats attendus, {s.fmp_daily_quota} requêtes/jour" if s.fmp_api_key else "non configuré (clé gratuite facultative)")

    async def load_universe(self) -> bool:
        try:
            self.universe.tickers = await self.ref_client.tickers_exchange()
            return True
        except Exception as ex:   # noqa: BLE001
            log.warning("liste des sociétés SEC indisponible : %r", ex)
            return False

    async def _universe_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=6 * 3600 if self.universe.loaded else 300)
            except asyncio.TimeoutError:
                if self.edgar_client:
                    await self.load_universe()

    async def start(self, *, background: bool = True) -> "Runtime":
        await self.build()
        if not background:
            return self
        if self.calendar.providers:
            try:
                await asyncio.wait_for(self.calendar.refresh(), timeout=60)
            except Exception as ex:   # noqa: BLE001
                log.warning("premier rafraîchissement du calendrier : %r", ex)
        coros = [self.reminders.run(self.stop_event), self.watchdog.run(self.stop_event), self._universe_loop()]
        if self.calendar.providers:
            coros.append(self.calendar.run(self.stop_event))
        if self.edgar:
            coros.append(self.edgar.run(self.stop_event))
        self.tasks = [asyncio.ensure_future(c) for c in coros]
        return self

    async def stop(self) -> None:
        self.stop_event.set()
        self.calendar.wakeup.set()
        if self.tasks:
            await asyncio.wait(self.tasks, timeout=10)
            for t in self.tasks:
                t.cancel()
        await self.pipeline.drain()
        await self.db.close()

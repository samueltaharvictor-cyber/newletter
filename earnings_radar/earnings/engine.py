"""Moteur de calendrier : rafraîchit les sources, consolide, versionne les révisions, suit les statuts."""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta

from ..app.serialize import event_to_api
from ..collectors.edgar import Universe
from ..core.clock import Clock
from ..core.domain import Change, EarningsEvent, EventStatus
from ..core.eventbus import EventBus
from ..core.marketcal import et_date
from ..db.repo import Repo
from ..net.http import HealthRegistry, QuotaExceeded
from ..quality.validators import reconcile_sources, validate_batch
from .logic import consolidate, diff_event, earliest_per_source, next_status, refresh_interval
from .providers import CalendarProvider

log = logging.getLogger("radar.calendar")


@dataclass
class RefreshReport:
    started_at: datetime
    window: tuple[date, date]
    sources_ok: list[str] = field(default_factory=list)
    sources_failed: dict[str, str] = field(default_factory=dict)
    observations: int = 0
    new_events: int = 0
    changes: list[dict] = field(default_factory=list)
    quality: dict = field(default_factory=dict)
    reconciliation: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"started_at": self.started_at.isoformat(), "window": [d.isoformat() for d in self.window],
                "sources_ok": self.sources_ok, "sources_failed": self.sources_failed, "observations": self.observations,
                "new_events": self.new_events, "changes": len(self.changes), "quality": self.quality,
                "reconciliation_items": len(self.reconciliation)}


class CalendarEngine:
    def __init__(self, repo: Repo, providers: list[CalendarProvider], universe: Universe, bus: EventBus, clock: Clock, *,
                 priority: list[str], lookahead_days: int = 30, health: HealthRegistry | None = None,
                 intervals: tuple[int, int, int] = (60, 15, 5)):
        self.repo, self.providers, self.universe, self.bus, self.clock = repo, providers, universe, bus, clock
        self.priority, self.lookahead, self.health, self.intervals = priority, lookahead_days, health, intervals
        self.last_report: RefreshReport | None = None
        self.today_tickers: set[str] = set()
        self.wakeup = asyncio.Event()

    async def _change(self, ev_id: int, ticker: str, ch: Change, now: datetime, report: RefreshReport | None = None) -> None:
        await self.repo.add_revision(ev_id, ticker, ch, now)
        if report is not None:
            report.changes.append({"ticker": ticker, "kind": ch.kind, "old": ch.old, "new": ch.new})
        ev = await self.repo.get_event(ev_id)
        await self.bus.publish("calendar_changed", {"ticker": ticker, "kind": ch.kind, "old": ch.old, "new": ch.new,
                                                    "event": event_to_api(ev) if ev else None})

    async def refresh(self) -> RefreshReport:
        now = self.clock.now()
        today = et_date(now)
        d_from, d_to = today, today + timedelta(days=self.lookahead)
        report = RefreshReport(started_at=now, window=(d_from, d_to))
        for p in self.providers:
            try:
                raw = await p.fetch(d_from, d_to)
            except QuotaExceeded as ex:
                report.sources_failed[p.name] = f"quota: {ex}"
                continue
            except Exception as ex:   # noqa: BLE001
                report.sources_failed[p.name] = f"{type(ex).__name__}: {ex}"[:200]
                if self.health:
                    self.health.error(p.name, "earnings_calendar", report.sources_failed[p.name])
                continue
            obs = [replace(o, observed_at=now) for o in raw if self.universe.accepts(o.ticker)]
            report.quality[p.name] = validate_batch(obs, today=today)
            await self.repo.replace_observations(p.name, d_from, d_to, obs, now)
            await self.repo.snapshot_consensus(obs, now)
            report.sources_ok.append(p.name)
            report.observations += len(obs)
            if self.health:
                self.health.ok(p.name, "earnings_calendar", data=True)
        self.last_report = report
        if not report.sources_ok:        # aucune source n'a répondu : on ne touche à rien (pas de fausses disparitions)
            await self.bus.publish("calendar_refreshed", report.to_dict())
            return report

        by_ticker: dict[str, list] = defaultdict(list)
        for o in await self.repo.observations_since(d_from):
            by_ticker[o.ticker].append(o)
        report.reconciliation = reconcile_sources({t: earliest_per_source(g, d_from) for t, g in by_ticker.items()})
        open_list = await self.repo.open_events()
        latest_open = {e.ticker: e for e in open_list}            # trié par date : la plus récente l'emporte
        reported = await self.repo.recently_reported(today - timedelta(days=5))
        seen_ids: set[int] = set()
        for ticker, group in by_ticker.items():
            rep = reported.get(ticker)
            cons = consolidate(earliest_per_source(group, rep.report_date + timedelta(days=4) if rep else d_from), self.priority)
            if cons is None:
                continue
            old = latest_open.get(ticker)
            if old is not None and old.report_date < today:       # date dépassée : la nouvelle date est un autre événement
                old = None
            changes = diff_event(old, cons)
            if old is None:
                det = await self.repo.recent_detected(ticker, "EARNINGS_RESULT", now - timedelta(hours=18))
                det_time = (det["published_at"] or det["received_at"]) if det else None
                if det and abs((cons.report_date - et_date(det_time)).days) <= 2:
                    ev_id = await self.repo.insert_event(cons, status=EventStatus.REPORTED, now=now, reported_at=det_time,
                                                         detected_event_id=det["id"])
                else:
                    status = EventStatus.PAST_DUE if cons.report_date < today else EventStatus.UPCOMING
                    ev_id = await self.repo.insert_event(cons, status=status, now=now)
                report.new_events += 1
            else:
                ev_id = old.id
                seen_ids.add(ev_id)
                await self.repo.update_event_from(ev_id, cons, now, changed=bool(changes))
            for ch in changes:
                await self._change(ev_id, ticker, ch, now, report)
        for e in open_list:
            st, missing = next_status(e, seen=e.id in seen_ids, covered=True, today=today)
            if (st, missing) == (e.status, e.missing_cycles):
                continue
            await self.repo.set_event_status(e.id, st, missing, now)
            kind = None
            if st == EventStatus.MISSING and e.status != EventStatus.MISSING:
                kind = "DISAPPEARED"
            elif e.status in (EventStatus.MISSING, EventStatus.PAST_DUE) and st == EventStatus.UPCOMING:
                kind = "REAPPEARED"
            elif st == EventStatus.PAST_DUE and e.status != EventStatus.PAST_DUE:
                kind = "PAST_DUE"
            if kind:
                await self._change(e.id, e.ticker, Change(kind, "status", e.status.value, st.value), now, report)
        self.today_tickers = {e.ticker for e in await self.repo.open_events() if e.report_date == today}
        await self.bus.publish("calendar_refreshed", report.to_dict())
        return report

    async def match(self, ticker: str, when: datetime, received_at: datetime) -> EarningsEvent | None:
        """Événement du calendrier correspondant à une publication, seulement s'il était connu AVANT la réception."""
        ev = await self.repo.latest_event_for(ticker)
        if ev and abs((ev.report_date - et_date(when)).days) <= 2 and ev.first_seen_at <= received_at:
            return ev
        return None

    async def mark_reported(self, ticker: str, detected_id: int, when: datetime) -> int | None:
        ev = await self.repo.latest_event_for(ticker)
        if ev is None or abs((ev.report_date - et_date(when)).days) > 2:
            return None
        if ev.status != EventStatus.REPORTED:
            await self.repo.mark_event_reported(ev.id, when, detected_id)
            await self._change(ev.id, ticker, Change("REPORTED", "status", ev.status.value, "REPORTED"), self.clock.now())
        return ev.id

    async def run(self, stop: asyncio.Event) -> None:
        idle, active, hot = self.intervals
        while not stop.is_set():
            try:
                await self.refresh()
            except Exception:   # noqa: BLE001
                log.exception("rafraîchissement du calendrier")
            wait = refresh_interval(self.clock.now(), await self.repo.open_events(), idle_min=idle, active_min=active,
                                    hot_min=hot).total_seconds()
            self.wakeup.clear()
            waiters = [asyncio.ensure_future(stop.wait()), asyncio.ensure_future(self.wakeup.wait())]
            try:
                await asyncio.wait(waiters, timeout=wait, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for w in waiters:
                    w.cancel()

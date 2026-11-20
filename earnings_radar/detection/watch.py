"""Rappels avant publication (heure exacte connue) et chien de garde des flux (surveillance de l'état du serveur)."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from ..collectors.edgar import edgar_poll_seconds
from ..core.clock import Clock
from ..core.eventbus import EventBus
from ..db.repo import Repo
from ..net.http import HealthRegistry
from ..notifications.notifiers import Alert, Notifier, build_reminder, build_watchdog

log = logging.getLogger("radar.watch")
LEADS = (60, 15)


async def _fanout(notifiers: list[Notifier], alert: Alert) -> None:
    async def one(n: Notifier) -> None:
        try:
            await asyncio.wait_for(n.send(alert), 15)
        except Exception as ex:   # noqa: BLE001
            log.warning("notification %s échouée : %r", n.name, ex)
    await asyncio.gather(*(one(n) for n in notifiers))


class Reminders:
    """Rappels à T-60 et T-15 min, uniquement quand une source fournit une HEURE EXACTE (sinon on n'invente rien)."""

    def __init__(self, repo: Repo, bus: EventBus, notifiers: list[Notifier], clock: Clock):
        self.repo, self.bus, self.notifiers, self.clock = repo, bus, notifiers, clock

    async def tick(self) -> int:
        now, sent = self.clock.now(), 0
        for ev in await self.repo.exact_time_events(now, now + timedelta(minutes=max(LEADS) + 1)):
            due = [lead for lead in LEADS if now >= ev.scheduled_at - timedelta(minutes=lead)]
            if not due:
                continue
            lead = min(due)
            alert = build_reminder(ev, lead)
            key = f"rem{lead}:{ev.id}:{ev.scheduled_at.isoformat()}"     # une heure modifiée ré-arme le rappel
            if await self.repo.insert_alert(key, "REMINDER", ev.id, ev.ticker, alert.title, alert.body,
                                            [n.name for n in self.notifiers], now) is None:
                continue
            await self.bus.publish("reminder", {"ticker": ev.ticker, "lead_min": lead, "title": alert.title, "body": alert.body})
            await _fanout(self.notifiers, alert)
            sent += 1
        return sent

    async def run(self, stop: asyncio.Event, every: float = 30.0) -> None:
        while not stop.is_set():
            try:
                await self.tick()
            except Exception:   # noqa: BLE001
                log.exception("rappels")
            try:
                await asyncio.wait_for(stop.wait(), timeout=every)
            except asyncio.TimeoutError:
                pass


class Watchdog:
    CRITICAL = (("sec_edgar", "filings_8k"),)

    def __init__(self, health: HealthRegistry, repo: Repo, bus: EventBus, notifiers: list[Notifier], clock: Clock,
                 stale_seconds: int = 180):
        self.health, self.repo, self.bus, self.notifiers, self.clock = health, repo, bus, notifiers, clock
        self.stale, self.started_at = stale_seconds, clock.now()
        self._open: dict[tuple[str, str], str] = {}

    async def tick(self) -> list[str]:
        now, events = self.clock.now(), []
        active = edgar_poll_seconds(now, 1.0) == 1.0         # fenêtre d'ouverture d'EDGAR
        for key in self.CRITICAL:
            h = self.health.get(*key)
            if h is None:
                continue
            age = (now - (h.last_ok_at or self.started_at)).total_seconds()
            if active and age > self.stale and key not in self._open:
                self._open[key] = now.isoformat()
                alert = build_watchdog(*key, age)
                events.append("stale")
            elif key in self._open and age <= self.stale:
                alert = build_watchdog(*key, 0, recovered=True)
                incident = self._open.pop(key)
                events.append("recovered")
                await self.repo.insert_alert(f"watchdog-ok:{key[0]}:{incident}", "WATCHDOG", None, None, alert.title, alert.body,
                                             [n.name for n in self.notifiers], now)
                await self.bus.publish("watchdog", {"provider": key[0], "state": "recovered", "title": alert.title})
                await _fanout(self.notifiers, alert)
                continue
            else:
                continue
            await self.repo.insert_alert(f"watchdog:{key[0]}:{self._open[key]}", "WATCHDOG", None, None, alert.title, alert.body,
                                         [n.name for n in self.notifiers], now)
            await self.bus.publish("watchdog", {"provider": key[0], "state": "stale", "title": alert.title, "body": alert.body})
            await _fanout(self.notifiers, alert)
        return events

    async def run(self, stop: asyncio.Event, every: float = 30.0) -> None:
        while not stop.is_set():
            try:
                await self.tick()
            except Exception:   # noqa: BLE001
                log.exception("chien de garde")
            try:
                await asyncio.wait_for(stop.wait(), timeout=every)
            except asyncio.TimeoutError:
                pass

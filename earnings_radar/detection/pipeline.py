"""Pipeline de détection : dédoublonne, relie au calendrier, persiste, publie dans l'appli puis notifie.
Chemin critique = base + bus ; les notifications externes partent en tâche de fond (jamais bloquantes) ; aucune IA."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta

from ..app.serialize import detected_to_api
from ..core.clock import Clock
from ..core.domain import EarningsSignal
from ..core.eventbus import EventBus
from ..db.repo import Repo
from ..notifications.notifiers import Notifier, build_alert

log = logging.getLogger("radar.pipeline")
DEDUP_WINDOW = timedelta(hours=18)


@dataclass(frozen=True)
class IngestResult:
    outcome: str          # created | enriched | duplicate
    event_id: int | None


class DetectionPipeline:
    def __init__(self, repo: Repo, bus: EventBus, calendar, notifiers: list[Notifier], clock: Clock, *,
                 notify_recovered: bool = False, notify_timeout: float = 15.0, follows_provider=None):
        self.repo, self.bus, self.calendar, self.notifiers, self.clock = repo, bus, calendar, notifiers, clock
        self.notify_recovered, self.notify_timeout = notify_recovered, notify_timeout
        self.follows_provider = follows_provider      # async () -> set[str] ; None = pousser pour tout le monde
        self._tasks: set[asyncio.Task] = set()

    async def ingest(self, sig: EarningsSignal) -> IngestResult:
        if sig.external_id and await self.repo.detected_by_external(sig.source, sig.external_id):
            return IngestResult("duplicate", None)
        simulated = sig.mode == "simulated"
        if sig.ticker and not simulated:
            existing = await self.repo.recent_detected(sig.ticker, sig.kind, sig.received_at - DEDUP_WINDOW)
            if existing and existing["source"] != sig.source:
                return await self._enrich(existing, sig)
        when = sig.published_at or sig.provider_at or sig.received_at
        cal = await self.calendar.match(sig.ticker, when, sig.received_at) if (sig.ticker and not simulated) else None
        ev_id = await self.repo.insert_detected(sig, scheduled=cal is not None, calendar_event_id=cal.id if cal else None,
                                                alert_at=self.clock.now())
        if ev_id is None:
            return IngestResult("duplicate", None)
        if sig.ticker and not simulated:
            await self.calendar.mark_reported(sig.ticker, ev_id, when)
        await self.bus.publish("event_detected", detected_to_api(await self.repo.get_detected(ev_id)))
        if simulated or sig.mode == "live" or self.notify_recovered:
            self._spawn(self._notify(ev_id))
        return IngestResult("created", ev_id)

    async def _enrich(self, existing: dict, sig: EarningsSignal) -> IngestResult:
        """Deuxième source pour une même publication : on complète, on ne ré-alerte pas."""
        if sig.source in existing["extra_sources"]:
            return IngestResult("duplicate", existing["id"])
        payload = dict(existing["payload"])
        payload.setdefault("confirmations", {})[sig.source] = {
            "external_id": sig.external_id, "url": sig.url, "received_at": sig.received_at.isoformat(),
            "published_at": sig.published_at.isoformat() if sig.published_at else None, "confidence": sig.confidence}
        fields = {"extra_sources": existing["extra_sources"] + [sig.source], "payload": payload}
        if sig.confidence == "filing" and existing["confidence"] != "filing":
            fields["confidence"] = "filing"          # le titre de presse est confirmé par un dépôt officiel
        await self.repo.update_detected(existing["id"], **fields)
        await self.bus.publish("event_updated", detected_to_api(await self.repo.get_detected(existing["id"])))
        return IngestResult("enriched", existing["id"])

    async def _notify(self, ev_id: int) -> None:
        row = await self.repo.get_detected(ev_id)
        if self.follows_provider is not None and row["detection_mode"] != "simulated":
            if row["ticker"] not in await self.follows_provider():
                return                                    # push externe uniquement pour les sociétés suivies
        alert = build_alert(row)
        created = await self.repo.insert_alert(f"stage1:{ev_id}", "DETECTED", ev_id, row["ticker"], alert.title, alert.body,
                                               [n.name for n in self.notifiers], self.clock.now())
        if created is None or not self.notifiers:
            return
        sent: list[str] = []

        async def one(n: Notifier) -> None:
            try:
                await asyncio.wait_for(n.send(alert), self.notify_timeout)
                sent.append(n.name)
            except Exception as ex:   # noqa: BLE001
                log.warning("notification %s échouée : %r", n.name, ex)

        await asyncio.gather(*(one(n) for n in self.notifiers))
        if sent:
            await self.repo.update_detected(ev_id, notified_at=self.clock.now())
            await self.bus.publish("event_updated", detected_to_api(await self.repo.get_detected(ev_id)))

    def _spawn(self, coro) -> None:
        t = asyncio.ensure_future(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

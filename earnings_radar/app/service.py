"""Modèles de lecture de l'API (aucun secret n'en sort) : statut, calendrier, événements, sociétés suivies, analytique."""
from __future__ import annotations

import re
from datetime import date, timedelta

from .. import __version__
from ..collectors.edgar import normalize_ticker
from ..core.domain import EarningsSignal, EventStatus
from ..core.latency import compute_latencies, summarize
from ..core.marketcal import TimeLabel, et_date, et_paris_offset_hours, market_state, tri_time
from .serialize import detected_to_api, event_to_api

_TICKER = re.compile(r"^[A-Z]{1,6}(-[A-Z]{1,2})?$")


class Service:
    def __init__(self, rt):
        self.rt = rt

    # ---------- calendrier ----------
    async def earnings(self, d_from: date, d_to: date, status: str | None = None) -> list[dict]:
        evs = await self.rt.repo.events_between(d_from, d_to)
        tm, out = self.rt.universe.tickers, []
        for e in evs:
            if status is not None and e.status.value != status:
                continue
            d = event_to_api(e)
            cik = tm.cik_for_ticker(e.ticker) if tm else None
            d["company"] = d["company"] or (tm.name_for_cik(cik) if (tm and cik) else None)
            out.append(d)
        return out

    async def earnings_today(self) -> list[dict]:
        today = et_date(self.rt.clock.now())
        return await self.earnings(today, today)

    @staticmethod
    def counts(events) -> dict:
        c = dict.fromkeys(("total", "confirmed", "estimated", "conflict", "no_time", "reported", "missing", "past_due"), 0)
        for e in events:
            c["total"] += 1
            c["reported"] += e.status == EventStatus.REPORTED
            c["missing"] += e.status == EventStatus.MISSING
            c["past_due"] += e.status == EventStatus.PAST_DUE
            if e.conflict:
                c["conflict"] += 1
            elif e.date_confirmed:
                c["confirmed"] += 1
            else:
                c["estimated"] += 1
            c["no_time"] += e.time_label == TimeLabel.UNKNOWN and e.scheduled_at is None
        return c

    # ---------- détections ----------
    async def events(self, limit: int = 100) -> list[dict]:
        return [detected_to_api(r) for r in await self.rt.repo.list_detected(limit)]

    async def revisions(self, limit: int = 50) -> list[dict]:
        return [{"id": r["id"], "event_id": r["event_id"], "ticker": r["ticker"], "kind": r["kind"], "field": r["field_name"],
                 "old": r["old_value"], "new": r["new_value"], "changed_at": r["changed_at"].isoformat(),
                 "times": tri_time(r["changed_at"])} for r in await self.rt.repo.revisions(limit)]

    async def _live_rows(self, days: int) -> list[dict]:
        rows = await self.rt.repo.detected_since(self.rt.clock.now() - timedelta(days=days))
        return [r for r in rows if r["detection_mode"] == "live" and r["source"] != "simulated" and r["kind"] == "EARNINGS_RESULT"]

    async def analytics(self) -> dict:
        rows = await self._live_rows(30)
        sched = sum(1 for r in rows if r["scheduled"])
        rep = self.rt.calendar.last_report
        return {
            "coverage": {"window_days": 30, "detected": len(rows), "scheduled": sched, "unscheduled": len(rows) - sched,
                         "rate": (sched / len(rows)) if rows else None,
                         "note": "part des résultats détectés en direct qui figuraient déjà au calendrier avant leur sortie"},
            "by_source": {s: sum(1 for r in rows if r["source"] == s) for s in {r["source"] for r in rows}},
            "reconciliation": rep.reconciliation if rep else [],
            "quality": rep.quality if rep else {},
            "latency_7d": summarize([compute_latencies(official=r["published_at"], provider=r["provider_at"], received=r["received_at"],
                                                       alert=r["alert_at"], notified=r["notified_at"])
                                     for r in await self._live_rows(7)]),
        }

    async def inject_test_event(self, ticker: str = "TEST") -> dict:
        now = self.rt.clock.now()
        t = (ticker or "TEST").upper()[:8]
        sig = EarningsSignal(
            source="simulated", kind="EARNINGS_RESULT", external_id=f"sim-{int(now.timestamp() * 1000)}", received_at=now,
            published_at=now - timedelta(seconds=1), ticker=t, company_name="Société de test",
            title=f"[TEST] {t} — résultats trimestriels (événement simulé)", url=None,
            confidence="simulated", mode="simulated", payload={"simulated": True})
        r = await self.rt.pipeline.ingest(sig)
        return {"outcome": r.outcome, "event_id": r.event_id}

    # ---------- sociétés suivies ----------
    async def follows(self) -> list[dict]:
        tm, out = self.rt.universe.tickers, []
        for t in await self.rt.repo.list_follows():
            ev = await self.rt.repo.latest_event_for(t)
            nxt = event_to_api(ev) if ev and ev.status in (EventStatus.UPCOMING, EventStatus.MISSING) else None
            cik = tm.cik_for_ticker(t) if tm else None
            out.append({"ticker": t, "name": tm.name_for_cik(cik) if (tm and cik) else None,
                        "exchange": tm.exchange_of(t) if tm else None, "next": nxt})
        return out

    async def follow(self, ticker: str) -> dict:
        t = normalize_ticker(ticker or "")
        if not _TICKER.match(t):
            raise ValueError("symbole invalide")
        if self.rt.universe.loaded and not self.rt.universe.accepts(t):
            raise LookupError("société introuvable sur le NYSE / Nasdaq")
        await self.rt.repo.add_follow(t, self.rt.clock.now())
        return {"ok": True, "follows": await self.follows()}

    async def unfollow(self, ticker: str) -> dict:
        await self.rt.repo.remove_follow(normalize_ticker(ticker or ""))
        return {"ok": True, "follows": await self.follows()}

    def search(self, q: str) -> list[dict]:
        tm = self.rt.universe.tickers
        return tm.search(q, self.rt.universe.exchanges) if tm else []

    # ---------- état ----------
    def degraded(self) -> list[str]:
        rt, s, out = self.rt, self.rt.settings, []
        if not s.sec_user_agent:
            out.append("Identification SEC manquante : sans elle, les résultats publiés ne peuvent pas être détectés (voir config.txt).")
        if not rt.calendar.providers:
            out.append("Pas de calendrier : ajoute une clé gratuite Finnhub dans config.txt pour voir les résultats attendus. "
                       "Les résultats publiés, eux, sont détectés.")
        if s.sec_user_agent and not rt.universe.loaded:
            out.append("Liste des sociétés SEC non chargée : la recherche de sociétés est indisponible pour le moment.")
        return out

    async def status(self) -> dict:
        rt, now = self.rt, self.rt.clock.now()
        ms, today = market_state(now), et_date(now)
        events = await rt.repo.events_between(today - timedelta(days=1), today + timedelta(days=rt.settings.calendar_lookahead_days))
        rep, rows7 = rt.calendar.last_report, await self._live_rows(7)
        fl = await self.follows()
        lat = summarize([compute_latencies(official=r["published_at"], provider=r["provider_at"], received=r["received_at"],
                                           alert=r["alert_at"], notified=r["notified_at"]) for r in rows7])
        return {
            "version": __version__, "now": tri_time(now), "started_at": rt.started_at.isoformat(),
            "market": {"session": ms.session.value, "trading_day": ms.trading_day, "early_close": ms.early_close,
                       "open": tri_time(ms.open_utc), "close": tri_time(ms.close_utc), "next_open": tri_time(ms.next_open_utc),
                       "paris_minus_ny_hours": et_paris_offset_hours(today)},
            "providers": rt.health.snapshot(),
            "calendar": {"last_refresh": rep.to_dict() if rep else None, "counts": self.counts(events),
                         "lookahead_days": rt.settings.calendar_lookahead_days,
                         "follows_without_date": [f["ticker"] for f in fl if f["next"] is None]},
            "detection": {"edgar_polls": rt.edgar.polls if rt.edgar else 0,
                          "events_today": sum(1 for r in rows7 if et_date(r["received_at"]) == today), "latency": lat},
            "universe": {"loaded": rt.universe.loaded, "size": len(rt.universe.tickers) if rt.universe.loaded else 0,
                         "exchanges": list(rt.universe.exchanges)},
            "channels": [n.name for n in rt.notifiers], "degraded": self.degraded(),
            "test_injection": rt.settings.enable_test_injection, "auth_required": bool(rt.settings.access_token),
            "clients": rt.bus.subscribers,
        }

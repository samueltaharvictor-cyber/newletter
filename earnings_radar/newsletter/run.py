"""Une passe de la newsletter : nouveaux dépôts 8-K « résultats » -> communiqué -> chiffre d'affaires en hausse ? -> notification.
Sans état en mémoire : tout ce qu'il faut retenir est dans un petit fichier JSON (donc exécutable par un planificateur gratuit)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from ..collectors.edgar import EdgarClient, parse_filing_meta
from ..config import Settings
from ..core.clock import Clock, SystemClock
from ..core.marketcal import PARIS
from ..net.http import ApiClient, RateLimiter, UrllibTransport
from ..notifications.notifiers import Alert, build_notifiers
from .analyze import analyze_release
from .exhibits import fetch_release_text

log = logging.getLogger("radar.newsletter")
UTC = timezone.utc
KEEP = 3000


def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            s = json.load(f)
        return {"watermark": s.get("watermark"), "done": list(s.get("done", []))}
    except (FileNotFoundError, ValueError):
        return {"watermark": None, "done": []}


def save_state(path: str, state: dict) -> None:
    state["done"] = state["done"][-KEEP:]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


class Newsletter:
    def __init__(self, edgar: EdgarClient, api: ApiClient, notifiers: list, clock: Clock, *, state_path: str = "newsletter_state.json",
                 journal_path: str = "journal.md", min_growth: float = 25.0, backfill_hours: float = 2.0, pages: int = 3):
        self.edgar, self.api, self.notifiers, self.clock = edgar, api, notifiers, clock
        self.state_path, self.journal_path, self.min_growth = state_path, journal_path, min_growth
        self.backfill, self.pages = backfill_hours, pages

    async def _entries(self, cutoff: datetime):
        out = []
        for p in range(self.pages):
            page = await self.edgar.current_8k(start=p * 100, count=100)
            out += page
            if len(page) < 100 or any(e.filed_at and e.filed_at < cutoff for e in page):
                break
        return out

    async def run_once(self) -> list[dict]:
        now, state = self.clock.now(), load_state(self.state_path)
        cutoff = (datetime.fromisoformat(state["watermark"]) - timedelta(minutes=30)) if state["watermark"] else now - timedelta(hours=self.backfill)
        entries = await self._entries(cutoff)
        done, sent = set(state["done"]), []
        for e in sorted(entries, key=lambda x: x.filed_at or now):
            if e.form != "8-K" or e.accession in done or (e.filed_at and e.filed_at < cutoff):
                continue
            try:
                sub = await self.edgar.submissions(e.cik)
                meta = parse_filing_meta(sub, e.accession)
            except Exception as ex:   # noqa: BLE001
                log.warning("%s : %r", e.accession, ex)
                continue
            if meta is None:                                     # pas encore visible : on réessaie à la prochaine passe
                if e.filed_at and now - e.filed_at < timedelta(hours=1):
                    continue
            elif meta.is_earnings:
                try:
                    got = await fetch_release_text(self.api, e.url)
                except Exception as ex:   # noqa: BLE001
                    log.warning("communiqué %s : %r", e.accession, ex)
                    continue
                res = analyze_release(got[0], self.min_growth) if got else None
                if res:
                    item = {"company": meta.name or e.company, "label": res.label, "sentence": res.sentence, "url": got[1],
                            "filed_at": e.filed_at, "kind": res.kind}
                    await self._announce(item, (sub.get("tickers") or [None])[0])
                    sent.append(item)
            done.add(e.accession)
            state["done"].append(e.accession)
        stamps = [e.filed_at for e in entries if e.filed_at]
        if stamps:
            state["watermark"] = max(stamps).isoformat()
        save_state(self.state_path, state)
        return sent

    async def send_test(self) -> int:
        """Envoie une notification de test sur les canaux configurés (vérifie le téléphone) ; renvoie le nombre de canaux atteints."""
        ok = 0
        for n in self.notifiers:
            try:
                await asyncio.wait_for(n.send(Alert("Test de la newsletter", "Si tu lis ceci sur ton téléphone, les alertes fonctionnent.", "default")), 15)
                ok += 1
            except Exception as ex:   # noqa: BLE001
                log.warning("test %s : %r", n.name, ex)
        return ok

    async def _announce(self, item: dict, ticker: str | None) -> None:
        who = f"{item['company']} ({ticker})" if ticker else item["company"]
        quote = item["sentence"] if len(item["sentence"]) < 240 else item["sentence"][:237] + "…"
        alert = Alert(f"Résultat en hausse — {who}", f"{item['label']}.\nExtrait du communiqué : « {quote} »\nSource : dépôt officiel SEC.", "high", item["url"])
        when = (item["filed_at"] or self.clock.now()).astimezone(PARIS).strftime("%Y-%m-%d %H:%M")
        with open(self.journal_path, "a", encoding="utf-8") as f:
            f.write(f"- {when} (Paris) — **{who}** : {item['label']} — [communiqué]({item['url']})\n")
        for n in self.notifiers:
            try:
                await asyncio.wait_for(n.send(alert), 15)
            except Exception as ex:   # noqa: BLE001
                log.warning("notification %s : %r", n.name, ex)


def build(settings: Settings, transport=None, clock: Clock | None = None) -> Newsletter:
    tr = transport or UrllibTransport()
    api = ApiClient("sec_edgar", "filings_8k", tr, headers={"User-Agent": settings.sec_user_agent}, limiter=RateLimiter(6))
    return Newsletter(EdgarClient(api), api, build_notifiers(settings, tr), clock or SystemClock(), state_path=settings.state_file,
                      journal_path=settings.journal_file, min_growth=settings.min_growth_pct, backfill_hours=settings.newsletter_backfill_hours)

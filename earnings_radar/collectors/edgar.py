"""SEC EDGAR : flux Atom « current events », API submissions, fichier tickers/bourses, et moniteur d'8-K Item 2.02.

Règles SEC respectées (documentation officielle) : User-Agent déclaré, 10 requêtes/s maximum (on reste à 8).
Chemin de détection : flux Atom 8-K (1 requête / ~5 s) -> API submissions de la société (champ `items`) -> Item 2.02
(= « Results of Operations and Financial Condition ») -> signal EARNINGS_RESULT, sans aucun appel IA.
"""
from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Awaitable, Callable, Iterable

from ..core.clock import Clock
from ..core.domain import EarningsSignal
from ..core.marketcal import NY
from ..net.http import ApiClient, HealthRegistry

log = logging.getLogger("radar.edgar")
UTC = timezone.utc
BASE, DATA = "https://www.sec.gov", "https://data.sec.gov"
ATOM = "{http://www.w3.org/2005/Atom}"
_TITLE = re.compile(r"^(?P<form>.+?)\s+-\s+(?P<name>.+?)\s+\((?P<cik>\d{10})\)\s+\((?P<role>[^)]*)\)\s*$")
_ACC = re.compile(r"accession-number=(\d{10}-\d{2}-\d{6})")


def normalize_ticker(t: str) -> str:
    return t.strip().upper().replace(".", "-")


def _iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.replace(tzinfo=UTC) if d.tzinfo is None else d.astimezone(UTC)


@dataclass(frozen=True)
class FeedEntry:
    accession: str
    cik: str
    company: str
    form: str
    role: str
    filed_at: datetime | None
    url: str | None


@dataclass(frozen=True)
class FilingMeta:
    accession: str
    cik: str
    name: str | None
    form: str | None
    items: tuple[str, ...]
    accepted_at: datetime | None
    filing_date: str | None
    primary_document: str | None
    description: str | None

    @property
    def is_earnings(self) -> bool:
        return "2.02" in self.items


def parse_current_feed(text: str) -> list[FeedEntry]:
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text.lstrip("\ufeff"))
    root = ET.fromstring(text)
    out: list[FeedEntry] = []
    for e in root.findall(f"{ATOM}entry"):
        m = _TITLE.match((e.findtext(f"{ATOM}title") or "").strip())
        am = _ACC.search(e.findtext(f"{ATOM}id") or "")
        if not m or not am:
            continue
        cat, link = e.find(f"{ATOM}category"), e.find(f"{ATOM}link")
        out.append(FeedEntry(
            accession=am.group(1), cik=m["cik"], company=m["name"], form=(cat.get("term") if cat is not None else m["form"]),
            role=m["role"], filed_at=_iso(e.findtext(f"{ATOM}updated")), url=link.get("href") if link is not None else None))
    return out


def parse_filing_meta(sub: dict, accession: str) -> FilingMeta | None:
    recent = (sub.get("filings") or {}).get("recent") or {}
    accs = recent.get("accessionNumber") or []
    if accession not in accs:
        return None
    i = accs.index(accession)

    def col(name: str):
        v = recent.get(name) or []
        return v[i] if i < len(v) else None

    items = col("items") or ""
    if isinstance(items, list):
        items = ",".join(items)
    return FilingMeta(
        accession=accession, cik=str(sub.get("cik", "")).zfill(10), name=sub.get("name"), form=col("form"),
        items=tuple(x.strip() for x in str(items).split(",") if x.strip()), accepted_at=_iso(col("acceptanceDateTime")),
        filing_date=col("filingDate"), primary_document=col("primaryDocument"), description=col("primaryDocDescription"))


class TickerMap:
    """CIK <-> ticker <-> bourse, d'après company_tickers_exchange.json (la SEC ne garantit ni exactitude ni exhaustivité)."""

    def __init__(self, rows: Iterable[tuple]):
        self._by_cik: dict[str, list[tuple[str, str]]] = {}
        self._exch: dict[str, str] = {}
        self._names: dict[str, str] = {}
        self._cik_of: dict[str, str] = {}
        self._rows: list[tuple[str, str, str]] = []
        for cik, name, ticker, exch in rows:
            c, t = f"{int(cik):010d}", normalize_ticker(ticker)
            self._by_cik.setdefault(c, []).append((t, exch))
            self._exch[t], self._names[c], self._cik_of[t] = exch, name, c
            self._rows.append((t, name, exch))

    @classmethod
    def from_sec_json(cls, obj: dict) -> "TickerMap":
        idx = {f: i for i, f in enumerate(obj["fields"])}
        return cls((r[idx["cik"]], r[idx["name"]], r[idx["ticker"]], r[idx["exchange"]]) for r in obj["data"] if r[idx["ticker"]])

    def __len__(self) -> int:
        return len(self._exch)

    def ticker_for_cik(self, cik: str, exchanges: Iterable[str] | None = None) -> str | None:
        rows = self._by_cik.get(cik.zfill(10), [])
        pref = [t for t, x in rows if exchanges is None or x in exchanges]
        return (pref or [t for t, _ in rows] or [None])[0]

    def has_cik(self, cik: str, exchanges: Iterable[str]) -> bool:
        return any(x in exchanges for _, x in self._by_cik.get(cik.zfill(10), []))

    def exchange_of(self, ticker: str) -> str | None:
        return self._exch.get(normalize_ticker(ticker))

    def cik_for_ticker(self, ticker: str) -> str | None:
        return self._cik_of.get(normalize_ticker(ticker))

    def search(self, q: str, exchanges: Iterable[str], limit: int = 8) -> list[dict]:
        """Recherche par début de ticker puis par morceau de nom."""
        q = q.strip().lower()
        if not q:
            return []
        pre, sub = [], []
        for t, name, ex in self._rows:
            if ex not in exchanges:
                continue
            if t.lower().startswith(q):
                pre.append((len(t), t, name, ex))
            elif q in name.lower():
                sub.append((len(name), t, name, ex))
        pre.sort()
        sub.sort()
        return [{"ticker": t, "name": n, "exchange": e} for _, t, n, e in (pre + sub)[:limit]]

    def name_for_cik(self, cik: str) -> str | None:
        return self._names.get(cik.zfill(10))


class Universe:
    """Périmètre NYSE/Nasdaq. Sans fichier tickers chargé, rien n'est filtré (et le statut le signale)."""

    def __init__(self, tickers: TickerMap | None, exchanges: Iterable[str]):
        self.tickers, self.exchanges = tickers, tuple(exchanges)

    @property
    def loaded(self) -> bool:
        return self.tickers is not None and len(self.tickers) > 0

    def accepts(self, ticker: str) -> bool:
        return True if not self.loaded else self.tickers.exchange_of(ticker) in self.exchanges

    def has_cik(self, cik: str) -> bool:
        return True if not self.loaded else self.tickers.has_cik(cik, self.exchanges)

    def ticker_for_cik(self, cik: str) -> str | None:
        return self.tickers.ticker_for_cik(cik, self.exchanges) if self.loaded else None


class EdgarClient:
    def __init__(self, api: ApiClient):
        self.api = api

    async def current_8k(self, start: int = 0, count: int = 100) -> list[FeedEntry]:
        r = await self.api.get_text(f"{BASE}/cgi-bin/browse-edgar", params={
            "action": "getcurrent", "type": "8-K", "company": "", "dateb": "", "owner": "include",
            "start": start, "count": count, "output": "atom"})
        return parse_current_feed(r.text)

    async def submissions(self, cik: str) -> dict:
        return await self.api.get_json(f"{DATA}/submissions/CIK{int(cik):010d}.json")

    async def tickers_exchange(self) -> TickerMap:
        return TickerMap.from_sec_json(await self.api.get_json(f"{BASE}/files/company_tickers_exchange.json"))


def edgar_poll_seconds(now: datetime, base: float) -> float:
    """Cadence rapide pendant les heures d'ouverture d'EDGAR (lun-ven, ~6h-22h ET), lente sinon."""
    ny = now.astimezone(NY)
    active = ny.weekday() < 5 and time(5, 55) <= ny.time() < time(22, 5)
    return base if active else max(base, 60.0)


IngestFn = Callable[[EarningsSignal], Awaitable[Any]]


class EdgarMonitor:
    MAX_ATTEMPTS = 6

    def __init__(self, client: EdgarClient, ingest: IngestFn, universe: Universe, repo, clock: Clock, *,
                 health: HealthRegistry | None = None, poll_seconds: float = 5.0, backfill_hours: float = 3.0,
                 recovery_pages: int = 3, priority_ciks: Callable[[], set[str]] | None = None):
        self.client, self.ingest, self.universe, self.repo, self.clock = client, ingest, universe, repo, clock
        self.health, self.poll_seconds, self.backfill_hours = health, poll_seconds, backfill_hours
        self.recovery_pages, self.priority_ciks = recovery_pages, priority_ciks or (lambda: set())
        self.started_at = clock.now()
        self._seen: set[str] = set()
        self._pending: dict[str, tuple[FeedEntry, int, datetime]] = {}
        self._cutoff: datetime | None = None
        self.polls = 0

    async def _compute_cutoff(self) -> datetime:
        wm = await self.repo.get_state("edgar.watermark")
        if wm:
            return datetime.fromisoformat(wm) - timedelta(minutes=10)
        return self.clock.now() - timedelta(hours=self.backfill_hours)

    def _retry(self, e: FeedEntry, recv: datetime, why: str) -> int:
        n = self._pending.get(e.accession, (e, 0, recv))[1] + 1
        if n >= self.MAX_ATTEMPTS:
            self._pending.pop(e.accession, None)
            log.warning("EDGAR %s abandonné après %d essais (%s)", e.accession, n, why)
        else:
            self._pending[e.accession] = (e, n, recv)
        return 0

    async def _process(self, e: FeedEntry, recv: datetime) -> int:
        try:
            sub = await self.client.submissions(e.cik)
        except Exception as ex:   # noqa: BLE001
            return self._retry(e, recv, f"submissions {type(ex).__name__}")
        meta = parse_filing_meta(sub, e.accession)
        if meta is None:          # l'API submissions peut avoir quelques secondes de retard sur le flux Atom
            return self._retry(e, recv, "absent de l'API submissions")
        self._pending.pop(e.accession, None)
        if not meta.is_earnings:
            return 0
        published = meta.accepted_at or e.filed_at
        recovered = published is not None and published < self.started_at - timedelta(seconds=60)
        url = e.url or f"{BASE}/Archives/edgar/data/{int(e.cik)}/{e.accession.replace('-', '')}/{e.accession}-index.htm"
        doc = (f"{BASE}/Archives/edgar/data/{int(e.cik)}/{e.accession.replace('-', '')}/{meta.primary_document}"
               if meta.primary_document else None)
        await self.ingest(EarningsSignal(
            source="sec_edgar_8k", kind="EARNINGS_RESULT", external_id=e.accession, received_at=recv, published_at=published,
            provider_at=e.filed_at if e.filed_at and e.filed_at != published else None,
            ticker=self.universe.ticker_for_cik(e.cik), cik=e.cik, company_name=meta.name or e.company, url=url,
            title=f"{meta.name or e.company} — 8-K, Item 2.02 (Results of Operations and Financial Condition)",
            confidence="filing", mode="recovered" if recovered else "live",
            payload={"form": meta.form, "items": list(meta.items), "primary_document_url": doc, "attempts": self._pending.get(e.accession, (0, 0))[1]}))
        return 1

    async def poll_once(self, pages: int = 1) -> int:
        recv = self.clock.now()
        if self._cutoff is None:
            self._cutoff = await self._compute_cutoff()
        entries: list[FeedEntry] = []
        for p in range(pages):
            page = await self.client.current_8k(start=p * 100, count=100)
            entries += page
            if len(page) < 100 or any(e.accession in self._seen for e in page):
                break
        todo: list[tuple[FeedEntry, datetime]] = []
        for e in entries:
            if e.accession in self._seen or e.form != "8-K":
                continue
            self._seen.add(e.accession)
            if e.filed_at and e.filed_at < self._cutoff:
                continue
            if self.universe.has_cik(e.cik):
                todo.append((e, recv))
        todo += [(e, r) for e, _, r in self._pending.values()]
        prio = self.priority_ciks()
        todo.sort(key=lambda x: (x[0].cik not in prio, x[0].filed_at or recv))
        emitted = 0
        for e, r in todo:
            emitted += await self._process(e, r)
        stamps = [e.filed_at for e in entries if e.filed_at]
        if stamps:
            await self.repo.set_state("edgar.watermark", max(stamps).isoformat(), recv)
        self.polls += 1
        if self.health:
            self.health.ok("sec_edgar", "filings_8k", data=bool(entries))
        return emitted

    async def run(self, stop: asyncio.Event) -> None:
        first = True
        while not stop.is_set():
            try:
                await self.poll_once(pages=self.recovery_pages if first else 1)
                first = False
            except Exception as ex:   # noqa: BLE001 — ne jamais laisser mourir la boucle de surveillance
                log.warning("EDGAR poll: %r", ex)
                if self.health:
                    self.health.error("sec_edgar", "filings_8k", f"{type(ex).__name__}: {ex}")
            try:
                await asyncio.wait_for(stop.wait(), timeout=edgar_poll_seconds(self.clock.now(), self.poll_seconds))
            except asyncio.TimeoutError:
                pass

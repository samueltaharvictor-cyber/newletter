"""Accès aux données métier (portable PostgreSQL / SQLite)."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from ..core.domain import Change, Consolidated, EarningsEvent, EarningsObservation, EarningsSignal, EventStatus
from ..core.marketcal import TimeLabel
from .core import Database

UTC = timezone.utc


def _ts(v) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, str):
        v = datetime.fromisoformat(v)
    return v.replace(tzinfo=UTC) if v.tzinfo is None else v.astimezone(UTC)


def _date(v) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def _b(v) -> bool | None:
    return None if v is None else bool(v)


def _event(r: dict) -> EarningsEvent:
    return EarningsEvent(
        id=r["id"], ticker=r["ticker"], company_name=r["company_name"], report_date=_date(r["report_date"]),
        time_label=TimeLabel(r["time_label"]), scheduled_at=_ts(r["scheduled_at"]), date_confirmed=bool(r["date_confirmed"]),
        time_confirmed=bool(r["time_confirmed"]), conflict=bool(r["conflict"]), status=EventStatus(r["status"]),
        sources=json.loads(r["sources"]), eps_estimate=r["eps_estimate"], revenue_estimate=r["revenue_estimate"],
        fiscal_year=r["fiscal_year"], fiscal_quarter=r["fiscal_quarter"], missing_cycles=r["missing_cycles"],
        first_seen_at=_ts(r["first_seen_at"]), last_seen_at=_ts(r["last_seen_at"]), last_changed_at=_ts(r["last_changed_at"]),
        reported_at=_ts(r["reported_at"]), detected_event_id=r["detected_event_id"])


def _observation(r: dict) -> EarningsObservation:
    return EarningsObservation(
        source=r["source"], ticker=r["ticker"], report_date=_date(r["report_date"]), time_label=TimeLabel(r["time_label"]),
        scheduled_at=_ts(r["scheduled_at"]), date_confirmed=_b(r["date_confirmed"]), company_name=r["company_name"],
        fiscal_year=r["fiscal_year"], fiscal_quarter=r["fiscal_quarter"], eps_estimate=r["eps_estimate"],
        revenue_estimate=r["revenue_estimate"], observed_at=_ts(r["observed_at"]))


def _detected(r: dict) -> dict:
    out = dict(r)
    for k in ("published_at", "provider_at", "received_at", "alert_at", "notified_at"):
        out[k] = _ts(r[k])
    out["scheduled"] = bool(r["scheduled"])
    out["payload"] = json.loads(r["payload"])
    out["extra_sources"] = json.loads(r["extra_sources"])
    return out


_EVENT_COLS = ("ticker, company_name, report_date, time_label, scheduled_at, date_confirmed, time_confirmed, conflict, "
               "status, sources, fiscal_year, fiscal_quarter, eps_estimate, revenue_estimate")
_DETECTED_UPDATABLE = {"notified_at", "alert_at", "confidence", "payload", "extra_sources", "calendar_event_id", "scheduled"}


class Repo:
    def __init__(self, db: Database):
        self.db = db

    # ---------- observations (dernier état par source) ----------
    async def replace_observations(self, source: str, d_from: date, d_to: date, obs: list[EarningsObservation],
                                   now: datetime) -> int:
        for o in obs:
            await self.db.execute(
                "INSERT INTO earnings_observations (source, ticker, report_date, time_label, scheduled_at, date_confirmed, "
                "company_name, fiscal_year, fiscal_quarter, eps_estimate, revenue_estimate, observed_at) VALUES "
                "(:source, :ticker, :report_date, :time_label, :scheduled_at, :date_confirmed, :company_name, :fiscal_year, "
                ":fiscal_quarter, :eps_estimate, :revenue_estimate, :observed_at) "
                "ON CONFLICT (source, ticker, report_date) DO UPDATE SET time_label = excluded.time_label, "
                "scheduled_at = excluded.scheduled_at, date_confirmed = excluded.date_confirmed, "
                "company_name = excluded.company_name, fiscal_year = excluded.fiscal_year, "
                "fiscal_quarter = excluded.fiscal_quarter, eps_estimate = excluded.eps_estimate, "
                "revenue_estimate = excluded.revenue_estimate, observed_at = excluded.observed_at",
                {"source": o.source, "ticker": o.ticker, "report_date": o.report_date, "time_label": o.time_label.value,
                 "scheduled_at": o.scheduled_at, "date_confirmed": o.date_confirmed, "company_name": o.company_name,
                 "fiscal_year": o.fiscal_year, "fiscal_quarter": o.fiscal_quarter, "eps_estimate": o.eps_estimate,
                 "revenue_estimate": o.revenue_estimate, "observed_at": now})
        # ce qui n'a pas été revu à ce cycle (dans la fenêtre) a disparu de la source
        await self.db.execute("DELETE FROM earnings_observations WHERE source = :s AND report_date >= :f AND "
                              "report_date <= :t AND observed_at < :now", {"s": source, "f": d_from, "t": d_to, "now": now})
        await self.db.execute("DELETE FROM earnings_observations WHERE report_date < :cut",
                              {"cut": d_from - timedelta(days=7)})
        return len(obs)

    async def observations_since(self, d_from: date) -> list[EarningsObservation]:
        rows = await self.db.fetch("SELECT * FROM earnings_observations WHERE report_date >= :f ORDER BY ticker, report_date",
                                   {"f": d_from})
        return [_observation(r) for r in rows]

    async def snapshot_consensus(self, obs: list[EarningsObservation], now: datetime) -> int:
        """Instantanés de consensus APPEND-ONLY, uniquement quand la valeur change (V2 §5 : jamais écrasés)."""
        rows = await self.db.fetch(
            "SELECT ticker, report_date, source, eps_estimate, revenue_estimate FROM consensus_snapshots WHERE id IN "
            "(SELECT MAX(id) FROM consensus_snapshots GROUP BY ticker, report_date, source)")
        last = {(r["ticker"], _date(r["report_date"]), r["source"]): (r["eps_estimate"], r["revenue_estimate"]) for r in rows}
        n = 0
        for o in obs:
            if o.eps_estimate is None and o.revenue_estimate is None:
                continue
            if last.get((o.ticker, o.report_date, o.source)) == (o.eps_estimate, o.revenue_estimate):
                continue
            await self.db.execute(
                "INSERT INTO consensus_snapshots (ticker, report_date, source, eps_estimate, revenue_estimate, captured_at) "
                "VALUES (:t, :d, :s, :e, :r, :c)",
                {"t": o.ticker, "d": o.report_date, "s": o.source, "e": o.eps_estimate, "r": o.revenue_estimate, "c": now})
            n += 1
        return n

    async def consensus_before(self, ticker: str, report_date: date, before: datetime) -> list[dict]:
        """Dernier consensus connu par source AVANT `before` (pas de biais rétrospectif)."""
        rows = await self.db.fetch(
            "SELECT source, eps_estimate, revenue_estimate, captured_at FROM consensus_snapshots WHERE id IN "
            "(SELECT MAX(id) FROM consensus_snapshots WHERE ticker = :t AND report_date = :d AND captured_at < :b "
            "GROUP BY source)", {"t": ticker, "d": report_date, "b": before})
        return [{**r, "captured_at": _ts(r["captured_at"])} for r in rows]

    # ---------- événements du calendrier ----------
    async def insert_event(self, c: Consolidated, *, status: EventStatus, now: datetime, reported_at: datetime | None = None,
                           detected_event_id: int | None = None) -> int:
        return await self.db.insert(
            f"INSERT INTO earnings_events ({_EVENT_COLS}, missing_cycles, first_seen_at, last_seen_at, last_changed_at, "
            "reported_at, detected_event_id) VALUES (:ticker, :company_name, :report_date, :time_label, :scheduled_at, "
            ":date_confirmed, :time_confirmed, :conflict, :status, :sources, :fiscal_year, :fiscal_quarter, :eps_estimate, "
            ":revenue_estimate, 0, :now, :now, :now, :reported_at, :det) RETURNING id",
            {"ticker": c.ticker, "company_name": c.company_name, "report_date": c.report_date, "time_label": c.time_label.value,
             "scheduled_at": c.scheduled_at, "date_confirmed": c.date_confirmed, "time_confirmed": c.time_confirmed,
             "conflict": c.conflict, "status": status.value, "sources": json.dumps(list(c.sources)),
             "fiscal_year": c.fiscal_year, "fiscal_quarter": c.fiscal_quarter, "eps_estimate": c.eps_estimate,
             "revenue_estimate": c.revenue_estimate, "now": now, "reported_at": reported_at, "det": detected_event_id})

    async def update_event_from(self, ev_id: int, c: Consolidated, now: datetime, *, changed: bool) -> None:
        await self.db.execute(
            "UPDATE earnings_events SET company_name = :cn, report_date = :d, time_label = :tl, scheduled_at = :sa, "
            "date_confirmed = :dc, time_confirmed = :tc, conflict = :cf, sources = :src, fiscal_year = :fy, "
            "fiscal_quarter = :fq, eps_estimate = :eps, revenue_estimate = :rev, last_seen_at = :now, "
            "last_changed_at = CASE WHEN :chg = 1 THEN :now ELSE last_changed_at END WHERE id = :id",
            {"cn": c.company_name, "d": c.report_date, "tl": c.time_label.value, "sa": c.scheduled_at,
             "dc": c.date_confirmed, "tc": c.time_confirmed, "cf": c.conflict, "src": json.dumps(list(c.sources)),
             "fy": c.fiscal_year, "fq": c.fiscal_quarter, "eps": c.eps_estimate, "rev": c.revenue_estimate, "now": now,
             "chg": 1 if changed else 0, "id": ev_id})

    async def set_event_status(self, ev_id: int, status: EventStatus, missing: int, now: datetime) -> None:
        await self.db.execute("UPDATE earnings_events SET status = :s, missing_cycles = :m, last_changed_at = :now "
                              "WHERE id = :id", {"s": status.value, "m": missing, "now": now, "id": ev_id})

    async def mark_event_reported(self, ev_id: int, reported_at: datetime, detected_id: int | None) -> None:
        await self.db.execute("UPDATE earnings_events SET status = 'REPORTED', reported_at = :r, detected_event_id = :d, "
                              "missing_cycles = 0, last_changed_at = :r WHERE id = :id",
                              {"r": reported_at, "d": detected_id, "id": ev_id})

    async def get_event(self, ev_id: int) -> EarningsEvent | None:
        r = await self.db.fetchone("SELECT * FROM earnings_events WHERE id = :id", {"id": ev_id})
        return _event(r) if r else None

    async def open_events(self) -> list[EarningsEvent]:
        rows = await self.db.fetch("SELECT * FROM earnings_events WHERE status IN ('UPCOMING', 'MISSING', 'PAST_DUE') "
                                   "ORDER BY report_date")
        return [_event(r) for r in rows]

    async def latest_event_for(self, ticker: str) -> EarningsEvent | None:
        r = await self.db.fetchone("SELECT * FROM earnings_events WHERE ticker = :t ORDER BY report_date DESC, id DESC LIMIT 1",
                                   {"t": ticker})
        return _event(r) if r else None

    async def recently_reported(self, since: date) -> dict[str, EarningsEvent]:
        rows = await self.db.fetch("SELECT * FROM earnings_events WHERE status = 'REPORTED' AND report_date >= :s "
                                   "ORDER BY report_date", {"s": since})
        return {e.ticker: e for e in map(_event, rows)}

    async def events_between(self, d_from: date, d_to: date) -> list[EarningsEvent]:
        rows = await self.db.fetch("SELECT * FROM earnings_events WHERE report_date >= :f AND report_date <= :t "
                                   "ORDER BY report_date, ticker", {"f": d_from, "t": d_to})
        return [_event(r) for r in rows]

    async def exact_time_events(self, t0: datetime, t1: datetime) -> list[EarningsEvent]:
        rows = await self.db.fetch("SELECT * FROM earnings_events WHERE status = 'UPCOMING' AND scheduled_at IS NOT NULL "
                                   "AND scheduled_at >= :a AND scheduled_at <= :b ORDER BY scheduled_at", {"a": t0, "b": t1})
        return [_event(r) for r in rows]

    # ---------- révisions ----------
    async def add_revision(self, event_id: int, ticker: str, ch: Change, now: datetime) -> None:
        await self.db.execute("INSERT INTO earnings_revisions (event_id, ticker, kind, field_name, old_value, new_value, "
                              "changed_at) VALUES (:e, :t, :k, :f, :o, :n, :c)",
                              {"e": event_id, "t": ticker, "k": ch.kind, "f": ch.field, "o": ch.old, "n": ch.new, "c": now})

    async def revisions(self, limit: int = 50) -> list[dict]:
        rows = await self.db.fetch("SELECT * FROM earnings_revisions ORDER BY changed_at DESC, id DESC LIMIT :n", {"n": limit})
        return [{**r, "changed_at": _ts(r["changed_at"])} for r in rows]

    # ---------- détections ----------
    async def insert_detected(self, sig: EarningsSignal, *, scheduled: bool, calendar_event_id: int | None,
                              alert_at: datetime) -> int | None:
        return await self.db.insert(
            "INSERT INTO detected_events (kind, ticker, cik, company_name, title, url, source, external_id, confidence, "
            "detection_mode, scheduled, calendar_event_id, published_at, provider_at, received_at, alert_at, notified_at, "
            "payload, extra_sources) VALUES (:kind, :ticker, :cik, :cn, :title, :url, :source, :ext, :conf, :mode, :sched, "
            ":cal, :pub, :prov, :recv, :alert, NULL, :payload, '[]') ON CONFLICT DO NOTHING RETURNING id",
            {"kind": sig.kind, "ticker": sig.ticker, "cik": sig.cik, "cn": sig.company_name, "title": sig.title, "url": sig.url,
             "source": sig.source, "ext": sig.external_id, "conf": sig.confidence, "mode": sig.mode, "sched": scheduled,
             "cal": calendar_event_id, "pub": sig.published_at, "prov": sig.provider_at, "recv": sig.received_at,
             "alert": alert_at, "payload": json.dumps(sig.payload, default=str)})

    async def get_detected(self, ev_id: int) -> dict | None:
        r = await self.db.fetchone("SELECT * FROM detected_events WHERE id = :id", {"id": ev_id})
        return _detected(r) if r else None

    async def detected_by_external(self, source: str, external_id: str | None) -> dict | None:
        if external_id is None:
            return None
        r = await self.db.fetchone("SELECT * FROM detected_events WHERE source = :s AND external_id = :e",
                                   {"s": source, "e": external_id})
        return _detected(r) if r else None

    async def known_external_ids(self, source: str, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        rows = await self.db.fetch("SELECT external_id FROM detected_events WHERE source = :s AND received_at >= :since",
                                   {"s": source, "since": datetime.now(UTC) - timedelta(days=2)})
        return {r["external_id"] for r in rows} & set(ids)

    async def recent_detected(self, ticker: str, kind: str, since: datetime) -> dict | None:
        r = await self.db.fetchone("SELECT * FROM detected_events WHERE ticker = :t AND kind = :k AND received_at >= :s "
                                   "AND detection_mode <> 'simulated' ORDER BY received_at DESC LIMIT 1",
                                   {"t": ticker, "k": kind, "s": since})
        return _detected(r) if r else None

    async def update_detected(self, ev_id: int, **fields) -> None:
        bad = set(fields) - _DETECTED_UPDATABLE
        if bad:
            raise ValueError(f"colonnes non modifiables : {bad}")
        vals = {k: (json.dumps(v, default=str) if k in ("payload", "extra_sources") else v) for k, v in fields.items()}
        sets = ", ".join(f"{k} = :{k}" for k in vals)
        await self.db.execute(f"UPDATE detected_events SET {sets} WHERE id = :id", {**vals, "id": ev_id})

    async def add_extra_source(self, ev_id: int, source: str) -> None:
        row = await self.get_detected(ev_id)
        if row and source not in row["extra_sources"] and source != row["source"]:
            await self.update_detected(ev_id, extra_sources=row["extra_sources"] + [source])

    async def list_detected(self, limit: int = 100) -> list[dict]:
        rows = await self.db.fetch("SELECT * FROM detected_events ORDER BY received_at DESC, id DESC LIMIT :n", {"n": limit})
        return [_detected(r) for r in rows]

    async def detected_since(self, since: datetime) -> list[dict]:
        rows = await self.db.fetch("SELECT * FROM detected_events WHERE received_at >= :s ORDER BY received_at", {"s": since})
        return [_detected(r) for r in rows]

    # ---------- état et alertes ----------
    async def get_state(self, key: str) -> str | None:
        r = await self.db.fetchone("SELECT value FROM monitor_state WHERE key = :k", {"k": key})
        return r["value"] if r else None

    async def set_state(self, key: str, value: str, now: datetime) -> None:
        await self.db.execute("INSERT INTO monitor_state (key, value, updated_at) VALUES (:k, :v, :t) "
                              "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                              {"k": key, "v": value, "t": now})

    async def insert_alert(self, dedup_key: str, stage: str, event_id: int | None, ticker: str | None, title: str,
                           body: str, channels: list[str], now: datetime) -> int | None:
        return await self.db.insert(
            "INSERT INTO alerts (dedup_key, stage, event_id, ticker, title, body, channels, created_at) VALUES "
            "(:k, :s, :e, :t, :ti, :b, :c, :now) ON CONFLICT DO NOTHING RETURNING id",
            {"k": dedup_key, "s": stage, "e": event_id, "t": ticker, "ti": title, "b": body, "c": json.dumps(channels), "now": now})

    async def recent_alerts(self, limit: int = 50) -> list[dict]:
        rows = await self.db.fetch("SELECT * FROM alerts ORDER BY created_at DESC, id DESC LIMIT :n", {"n": limit})
        return [{**r, "created_at": _ts(r["created_at"]), "channels": json.loads(r["channels"])} for r in rows]

    # ---------- sociétés suivies ----------
    async def add_follow(self, ticker: str, now: datetime) -> None:
        await self.db.execute("INSERT INTO follows (ticker, added_at) VALUES (:t, :n) ON CONFLICT (ticker) DO NOTHING",
                              {"t": ticker, "n": now})

    async def remove_follow(self, ticker: str) -> None:
        await self.db.execute("DELETE FROM follows WHERE ticker = :t", {"t": ticker})

    async def list_follows(self) -> list[str]:
        return [r["ticker"] for r in await self.db.fetch("SELECT ticker FROM follows ORDER BY added_at, ticker")]

    async def follows_set(self) -> set[str]:
        return set(await self.list_follows())

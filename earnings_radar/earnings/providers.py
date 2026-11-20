"""Sources GRATUITES de calendrier d'earnings (clés gratuites, usage personnel). Chaque `parse_*` est pur et testé."""
from __future__ import annotations

from datetime import date
from typing import Any, Protocol

from ..collectors.edgar import normalize_ticker
from ..core.domain import EarningsObservation
from ..core.marketcal import TimeLabel
from ..net.http import ApiClient

_LABELS = {"bmo": TimeLabel.BMO, "amc": TimeLabel.AMC, "dmh": TimeLabel.INTRA,
           "before market open": TimeLabel.BMO, "after market close": TimeLabel.AMC, "during market hours": TimeLabel.INTRA}


class CalendarProvider(Protocol):
    name: str
    async def fetch(self, d_from: date, d_to: date) -> list[EarningsObservation]: ...


def _num(v: Any) -> float | None:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    n = _num(v)
    return None if n is None else int(n)


def _first(row: dict, *keys: str) -> Any:
    return next((row[k] for k in keys if row.get(k) not in (None, "")), None)


def _label(v: Any) -> TimeLabel:
    return _LABELS.get(str(v or "").strip().lower(), TimeLabel.UNKNOWN)


def _d(v: Any) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def parse_finnhub(payload: dict, source: str = "finnhub") -> list[EarningsObservation]:
    out = []
    for r in payload.get("earningsCalendar") or []:
        d, sym = _d(r.get("date")), r.get("symbol")
        if d is None or not sym:
            continue
        out.append(EarningsObservation(
            source=source, ticker=normalize_ticker(sym), report_date=d, time_label=_label(r.get("hour")),
            fiscal_year=_int(r.get("year")), fiscal_quarter=_int(r.get("quarter")),
            eps_estimate=_num(r.get("epsEstimate")), revenue_estimate=_num(r.get("revenueEstimate"))))
    return out


def parse_fmp(rows: list[dict], source: str = "fmp") -> list[EarningsObservation]:
    out = []
    for r in rows or []:
        d, sym = _d(r.get("date")), r.get("symbol")
        if d is None or not sym:
            continue
        out.append(EarningsObservation(
            source=source, ticker=normalize_ticker(sym), report_date=d,
            time_label=_label(_first(r, "time", "timing", "hour", "releaseTime")),   # champ non garanti : voir `check`
            eps_estimate=_num(_first(r, "epsEstimated", "epsEstimate")),
            revenue_estimate=_num(_first(r, "revenueEstimated", "revenueEstimate"))))
    return out


class FinnhubCalendar:
    name = "finnhub"

    def __init__(self, api: ApiClient, key: str):
        self.api, self.key = api, key

    async def fetch(self, d_from: date, d_to: date) -> list[EarningsObservation]:
        return parse_finnhub(await self.api.get_json("https://finnhub.io/api/v1/calendar/earnings", params={
            "from": d_from.isoformat(), "to": d_to.isoformat(), "token": self.key}))


class FmpCalendar:
    name = "fmp"

    def __init__(self, api: ApiClient, key: str):
        self.api, self.key = api, key

    async def fetch(self, d_from: date, d_to: date) -> list[EarningsObservation]:
        return parse_fmp(await self.api.get_json("https://financialmodelingprep.com/stable/earnings-calendar", params={
            "from": d_from.isoformat(), "to": d_to.isoformat(), "apikey": self.key}))

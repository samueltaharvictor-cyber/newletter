"""Calendrier NYSE/Nasdaq, sessions, conversions ET / Paris / UTC.

Jours fériés calculés par règle, recoupés dans les tests avec le calendrier officiel NYSE
2026-2028 (annonce ICE du 23/12/2025). À revérifier chaque année.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from functools import lru_cache
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
PARIS = ZoneInfo("Europe/Paris")
UTC = timezone.utc

PRE_OPEN = time(4, 0)
OPEN = time(9, 30)
CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)
POST_END = time(20, 0)
POST_END_EARLY = time(17, 0)  # sessions tardives NYSE Arca/American les jours de clôture anticipée
EXTRA_CLOSURES = frozenset({date(2025, 1, 9)})  # journée de deuil national


class Session(str, Enum):
    CLOSED = "CLOSED"
    PREMARKET = "PREMARKET"
    REGULAR = "REGULAR"
    AFTER_HOURS = "AFTER_HOURS"


class TimeLabel(str, Enum):
    BMO = "BMO"          # avant l'ouverture
    INTRA = "INTRA"      # pendant la séance
    AMC = "AMC"          # après la clôture
    UNKNOWN = "UNKNOWN"


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> date:  # algorithme grégorien anonyme
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(d: date) -> date:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


@lru_cache(maxsize=None)
def holidays(year: int) -> frozenset[date]:
    days = {
        _nth_weekday(year, 1, 0, 3),            # Martin Luther King Jr.
        _nth_weekday(year, 2, 0, 3),            # Washington's Birthday
        _easter(year) - timedelta(days=2),      # Good Friday
        _last_weekday(year, 5, 0),              # Memorial Day
        _nth_weekday(year, 9, 0, 1),            # Labor Day
        _nth_weekday(year, 11, 3, 4),           # Thanksgiving
        _observed(date(year, 7, 4)),
        _observed(date(year, 12, 25)),
    }
    if date(year, 1, 1).weekday() != 5:         # 1er janv. un samedi : aucun jour férié compensé
        days.add(_observed(date(year, 1, 1)))
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))
    return frozenset(days)


@lru_cache(maxsize=None)
def early_closes(year: int) -> frozenset[date]:
    out = {_nth_weekday(year, 11, 3, 4) + timedelta(days=1)}   # lendemain de Thanksgiving
    d24 = date(year, 12, 24)
    if d24.weekday() < 5 and d24 not in holidays(year):
        out.add(d24)
    j3 = date(year, 7, 3)
    if j3.weekday() < 4 and j3 not in holidays(year):          # lundi-jeudi, veille du 4 juillet
        out.add(j3)
    return frozenset(out)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in holidays(d.year) and d not in EXTRA_CLOSURES


def close_time(d: date) -> time:
    return EARLY_CLOSE if d in early_closes(d.year) else CLOSE


def next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def prev_trading_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def at_et(d: date, t: time) -> datetime:
    """Heure locale New York -> datetime UTC."""
    return datetime.combine(d, t, tzinfo=NY).astimezone(UTC)


def et_date(ts: datetime) -> date:
    return ts.astimezone(NY).date()


@dataclass(frozen=True)
class MarketState:
    session: Session
    trading_day: bool
    early_close: bool
    open_utc: datetime | None
    close_utc: datetime | None
    next_open_utc: datetime


def market_state(ts: datetime) -> MarketState:
    ny = ts.astimezone(NY)
    d = ny.date()
    td = is_trading_day(d)
    t = ny.time()
    session = Session.CLOSED
    early = d in early_closes(d.year) if td else False
    if td:
        c = close_time(d)
        post_end = POST_END_EARLY if early else POST_END
        if PRE_OPEN <= t < OPEN:
            session = Session.PREMARKET
        elif OPEN <= t < c:
            session = Session.REGULAR
        elif c <= t < post_end:
            session = Session.AFTER_HOURS
    next_open = at_et(d, OPEN) if (td and t < OPEN) else at_et(next_trading_day(d), OPEN)
    return MarketState(
        session=session, trading_day=td, early_close=early,
        open_utc=at_et(d, OPEN) if td else None,
        close_utc=at_et(d, close_time(d)) if td else None,
        next_open_utc=next_open,
    )


def classify_release(ts: datetime) -> TimeLabel | None:
    """BMO / INTRA / AMC pour un instant donné ; None si ce n'est pas un jour de bourse.
    Une publication exactement à la clôture (16:00:00) est classée AMC."""
    ny = ts.astimezone(NY)
    d = ny.date()
    if not is_trading_day(d):
        return None
    if ny.time() < OPEN:
        return TimeLabel.BMO
    if ny.time() < close_time(d):
        return TimeLabel.INTRA
    return TimeLabel.AMC


def label_window(d: date, label: TimeLabel) -> tuple[datetime, datetime] | None:
    """Fenêtre UTC couverte par un libellé BMO/INTRA/AMC (affichage, pas une heure confirmée)."""
    if not is_trading_day(d) or label == TimeLabel.UNKNOWN:
        return None
    c = close_time(d)
    post_end = POST_END_EARLY if c == EARLY_CLOSE else POST_END
    if label == TimeLabel.BMO:
        return at_et(d, PRE_OPEN), at_et(d, OPEN)
    if label == TimeLabel.INTRA:
        return at_et(d, OPEN), at_et(d, c)
    return at_et(d, c), at_et(d, post_end)


def tri_time(ts: datetime | None) -> dict | None:
    """Même instant en UTC, New York et Paris (avec abréviation de fuseau)."""
    if ts is None:
        return None
    ts = ts.astimezone(UTC)
    f = "%Y-%m-%d %H:%M:%S %Z"
    return {"utc": ts.isoformat(), "et": ts.astimezone(NY).strftime(f), "paris": ts.astimezone(PARIS).strftime(f)}


def et_paris_offset_hours(d: date) -> float:
    """Décalage Paris - New York (5, 6 ou 7 h selon les passages à l'heure d'été)."""
    ref = datetime(d.year, d.month, d.day, 12, tzinfo=UTC)
    return (ref.astimezone(PARIS).utcoffset() - ref.astimezone(NY).utcoffset()).total_seconds() / 3600

"""Logique pure du calendrier : consolidation multi-sources, détection de révisions, statuts, cadence de rafraîchissement."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Sequence

from ..core.domain import Change, Consolidated, EarningsEvent, EarningsObservation, EventStatus
from ..core.marketcal import TimeLabel, classify_release, et_date

MISSING_THRESHOLD = 3   # cycles consécutifs sans trace avant de signaler une disparition


def _rank(source: str, priority: Sequence[str]) -> int:
    return priority.index(source) if source in priority else len(priority)


def earliest_per_source(obs: Sequence[EarningsObservation], floor: date) -> list[EarningsObservation]:
    """Hypothèse : une seule publication à venir par société et par source (la plus proche)."""
    best: dict[str, EarningsObservation] = {}
    for o in obs:
        if o.report_date < floor:
            continue
        cur = best.get(o.source)
        if cur is None or o.report_date < cur.report_date:
            best[o.source] = o
    return list(best.values())


def consolidate(observations: Sequence[EarningsObservation], priority: Sequence[str]) -> Consolidated | None:
    if not observations:
        return None
    obs = sorted(observations, key=lambda o: (_rank(o.source, priority), o.source))
    head = obs[0]
    same = [o for o in obs if o.report_date == head.report_date]
    conflict = len({o.report_date for o in obs}) > 1

    def pick(attr):
        return next((getattr(o, attr) for o in same if getattr(o, attr) is not None), None)

    # une heure exacte dont la date new-yorkaise ne correspond pas à la date annoncée est une donnée douteuse : ignorée
    scheduled = next((o.scheduled_at for o in same if o.scheduled_at is not None
                      and et_date(o.scheduled_at) == head.report_date), None)
    label = next((o.time_label for o in same if o.time_label != TimeLabel.UNKNOWN), TimeLabel.UNKNOWN)
    if label == TimeLabel.UNKNOWN and scheduled is not None:
        label = classify_release(scheduled) or TimeLabel.UNKNOWN
    date_confirmed = any(o.date_confirmed is True for o in same)
    return Consolidated(
        ticker=head.ticker, company_name=pick("company_name"), report_date=head.report_date,
        time_label=label, scheduled_at=scheduled, date_confirmed=date_confirmed,
        time_confirmed=bool(date_confirmed and scheduled is not None), conflict=conflict,
        sources=tuple(sorted(o.source for o in same)), eps_estimate=pick("eps_estimate"),
        revenue_estimate=pick("revenue_estimate"), fiscal_year=pick("fiscal_year"), fiscal_quarter=pick("fiscal_quarter"),
    )


def _s(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    return v.isoformat() if isinstance(v, (date, datetime)) else getattr(v, "value", str(v))


def diff_event(old: EarningsEvent | None, new: Consolidated) -> list[Change]:
    if old is None:
        return [Change("NEW_EVENT", None, None, _s(new.report_date))]
    ch: list[Change] = []
    if old.report_date != new.report_date:
        ch.append(Change("DATE_CHANGED", "report_date", _s(old.report_date), _s(new.report_date)))
    if old.time_label != new.time_label:
        ch.append(Change("TIME_LABEL_CHANGED", "time_label", _s(old.time_label), _s(new.time_label)))
    if old.scheduled_at != new.scheduled_at:
        ch.append(Change("TIME_CHANGED", "scheduled_at", _s(old.scheduled_at), _s(new.scheduled_at)))
    if new.date_confirmed != old.date_confirmed:
        ch.append(Change("DATE_CONFIRMED" if new.date_confirmed else "DATE_UNCONFIRMED", "date_confirmed",
                         _s(old.date_confirmed), _s(new.date_confirmed)))
    if new.time_confirmed != old.time_confirmed:
        ch.append(Change("TIME_CONFIRMED" if new.time_confirmed else "TIME_UNCONFIRMED", "time_confirmed",
                         _s(old.time_confirmed), _s(new.time_confirmed)))
    if new.conflict != old.conflict:
        ch.append(Change("CONFLICT" if new.conflict else "CONFLICT_CLEARED", "conflict", _s(old.conflict), _s(new.conflict)))
    return ch


def next_status(ev: EarningsEvent, *, seen: bool, covered: bool, today: date) -> tuple[EventStatus, int]:
    """Statut / compteur de cycles manquants après un cycle de rafraîchissement.
    seen = la société figure dans les sources ce cycle ; covered = au moins une source a répondu."""
    if ev.status == EventStatus.REPORTED:
        return EventStatus.REPORTED, 0
    if seen:
        return (EventStatus.PAST_DUE if ev.report_date < today else EventStatus.UPCOMING), 0
    if not covered:
        return ev.status, ev.missing_cycles
    missing = ev.missing_cycles + 1
    if ev.report_date < today:
        return EventStatus.PAST_DUE, missing
    return (EventStatus.MISSING if missing >= MISSING_THRESHOLD else ev.status), missing


def refresh_interval(now: datetime, events: Sequence[EarningsEvent], *, idle_min: int, active_min: int,
                     hot_min: int) -> timedelta:
    """Cadence : idle (rien aujourd'hui) < active (publications attendues aujourd'hui) < hot (heure exacte proche)."""
    today = et_date(now)
    todays = [e for e in events if e.report_date == today and e.status == EventStatus.UPCOMING]
    if not todays:
        return timedelta(minutes=idle_min)
    for e in todays:
        if e.scheduled_at and e.scheduled_at - timedelta(minutes=60) <= now <= e.scheduled_at + timedelta(minutes=45):
            return timedelta(minutes=hot_min)
    return timedelta(minutes=active_min)

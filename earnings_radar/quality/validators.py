"""Contrôles qualité des données entrantes (skills data : validate-data / explore-data) et réconciliation inter-sources."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Mapping, Sequence

from ..core.domain import EarningsObservation
from ..core.marketcal import TimeLabel, classify_release, et_date, is_trading_day

_TICKER = re.compile(r"^[A-Z]{1,5}(-[A-Z]{1,2})?$")


@dataclass(frozen=True)
class Issue:
    code: str
    severity: str        # error | warn
    message: str
    ticker: str | None = None


def validate_observation(o: EarningsObservation, *, today: date) -> list[Issue]:
    out: list[Issue] = []
    add = lambda code, sev, msg: out.append(Issue(code, sev, msg, o.ticker))
    if not _TICKER.match(o.ticker):
        add("TICKER_FORMAT", "warn", f"symbole inhabituel : {o.ticker!r}")
    if abs((o.report_date - today).days) > 400:
        add("DATE_RANGE", "error", f"date hors plage plausible : {o.report_date}")
    if o.report_date.weekday() >= 5 or not is_trading_day(o.report_date):
        add("NON_TRADING_DAY", "warn", f"{o.report_date} n'est pas un jour de bourse")
    if o.scheduled_at is not None:
        if et_date(o.scheduled_at) != o.report_date:
            add("TIME_DATE_MISMATCH", "error", "heure exacte et date annoncée incohérentes (fuseau ?)")
        else:
            lab = classify_release(o.scheduled_at)
            if lab and o.time_label not in (TimeLabel.UNKNOWN, lab):
                add("LABEL_TIME_MISMATCH", "warn", f"libellé {o.time_label.value} ≠ heure exacte ({lab.value})")
    if o.fiscal_quarter is not None and not 1 <= o.fiscal_quarter <= 4:
        add("FISCAL_QUARTER", "error", f"trimestre fiscal invalide : {o.fiscal_quarter}")
    if o.eps_estimate is not None and abs(o.eps_estimate) > 100:
        add("EPS_MAGNITUDE", "warn", f"BPA attendu suspect ({o.eps_estimate}) : unité ou devise ?")
    if o.revenue_estimate is not None:
        if o.revenue_estimate < 0:
            add("REVENUE_NEGATIVE", "error", "chiffre d'affaires attendu négatif")
        elif 0 < o.revenue_estimate < 1_000:
            add("REVENUE_UNITS", "warn", f"CA attendu {o.revenue_estimate} : exprimé en millions ? (unités à vérifier)")
    return out


def validate_batch(obs: Sequence[EarningsObservation], *, today: date) -> dict:
    issues = [i for o in obs for i in validate_observation(o, today=today)]
    dup = Counter((o.source, o.ticker, o.report_date) for o in obs)
    return {
        "n": len(obs),
        "by_code": dict(Counter(i.code for i in issues)),
        "errors": sum(1 for i in issues if i.severity == "error"),
        "duplicates": sum(1 for c in dup.values() if c > 1),
        "null_rate": {
            "time_label": _rate(sum(1 for o in obs if o.time_label == TimeLabel.UNKNOWN), len(obs)),
            "eps_estimate": _rate(sum(1 for o in obs if o.eps_estimate is None), len(obs)),
            "revenue_estimate": _rate(sum(1 for o in obs if o.revenue_estimate is None), len(obs)),
        },
    }


def _rate(n: int, total: int) -> float | None:
    return None if not total else n / total


def reconcile_sources(by_ticker: Mapping[str, Iterable[EarningsObservation]], *, eps_gap_pct: float = 20.0) -> list[dict]:
    """Éléments de réconciliation entre sources (écarts de date, de libellé, de BPA attendu)."""
    items: list[dict] = []
    for ticker, group in by_ticker.items():
        g = list(group)
        if len(g) < 2:
            continue
        dates = {o.source: o.report_date.isoformat() for o in g}
        if len(set(dates.values())) > 1:
            items.append({"ticker": ticker, "kind": "DATE_MISMATCH", "detail": dates})
        labels = {o.source: o.time_label.value for o in g if o.time_label != TimeLabel.UNKNOWN}
        if len(set(labels.values())) > 1:
            items.append({"ticker": ticker, "kind": "LABEL_MISMATCH", "detail": labels})
        eps = {o.source: o.eps_estimate for o in g if o.eps_estimate is not None}
        if len(eps) >= 2:
            lo, hi = min(eps.values()), max(eps.values())
            base = max(abs(lo), abs(hi))
            if base > 1e-9 and (hi - lo) / base * 100 > eps_gap_pct:
                items.append({"ticker": ticker, "kind": "EPS_ESTIMATE_GAP", "detail": eps})
    return items

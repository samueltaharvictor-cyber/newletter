"""Objets métier (purs, sans I/O)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum

from .marketcal import TimeLabel  # noqa: F401  (ré-export)


class EventStatus(str, Enum):
    UPCOMING = "UPCOMING"
    REPORTED = "REPORTED"
    MISSING = "MISSING"      # a disparu des sources alors que la date n'est pas passée (annulation ? report ?)
    PAST_DUE = "PAST_DUE"    # date dépassée sans publication détectée


@dataclass(frozen=True)
class EarningsObservation:
    """Ce qu'UNE source dit d'une publication, à un instant donné."""
    source: str
    ticker: str
    report_date: date                          # date calendaire à New York
    time_label: TimeLabel = TimeLabel.UNKNOWN
    scheduled_at: datetime | None = None       # UTC ; seulement si la source donne une heure exacte
    date_confirmed: bool | None = None         # None = la source ne dit rien
    company_name: str | None = None
    fiscal_year: int | None = None
    fiscal_quarter: int | None = None
    eps_estimate: float | None = None
    revenue_estimate: float | None = None
    observed_at: datetime | None = None


@dataclass(frozen=True)
class Consolidated:
    """Vue consolidée multi-sources d'une publication à venir."""
    ticker: str
    company_name: str | None
    report_date: date
    time_label: TimeLabel
    scheduled_at: datetime | None
    date_confirmed: bool
    time_confirmed: bool
    conflict: bool
    sources: tuple[str, ...]
    eps_estimate: float | None
    revenue_estimate: float | None
    fiscal_year: int | None
    fiscal_quarter: int | None


@dataclass
class EarningsEvent:
    id: int
    ticker: str
    company_name: str | None
    report_date: date
    time_label: TimeLabel
    scheduled_at: datetime | None
    date_confirmed: bool
    time_confirmed: bool
    conflict: bool
    status: EventStatus
    sources: list[str]
    eps_estimate: float | None
    revenue_estimate: float | None
    fiscal_year: int | None
    fiscal_quarter: int | None
    missing_cycles: int
    first_seen_at: datetime
    last_seen_at: datetime
    last_changed_at: datetime
    reported_at: datetime | None = None
    detected_event_id: int | None = None


@dataclass(frozen=True)
class Change:
    kind: str
    field: str | None
    old: str | None
    new: str | None


@dataclass
class EarningsSignal:
    """Entrée du pipeline de détection (EDGAR, flux d'actualités, simulation...)."""
    source: str                    # sec_edgar_8k | simulated
    kind: str                      # EARNINGS_RESULT
    title: str
    external_id: str | None
    received_at: datetime
    published_at: datetime | None = None   # heure officielle (acceptation SEC)
    provider_at: datetime | None = None    # heure de publication chez le fournisseur
    ticker: str | None = None
    cik: str | None = None
    company_name: str | None = None
    url: str | None = None
    confidence: str = "headline"           # filing | headline | simulated
    mode: str = "live"                     # live | recovered | simulated
    payload: dict = field(default_factory=dict)

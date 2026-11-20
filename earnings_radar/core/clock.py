"""Horloge injectable (testable) — toutes les heures internes sont en UTC."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

UTC = timezone.utc


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    def __init__(self, start: datetime):
        self._t = start

    def now(self) -> datetime:
        return self._t

    def set(self, t: datetime) -> None:
        self._t = t

    def advance(self, **kw) -> None:
        self._t += timedelta(**kw)

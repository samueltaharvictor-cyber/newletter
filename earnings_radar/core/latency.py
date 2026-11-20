"""Mesure de latence : retard source, traitement interne, notification (chaînes de V2 §4)."""
from __future__ import annotations

from datetime import datetime


def _ms(a: datetime | None, b: datetime | None) -> float | None:
    return None if a is None or b is None else (b - a).total_seconds() * 1000.0


def compute_latencies(*, official: datetime | None, provider: datetime | None, received: datetime | None,
                      alert: datetime | None, notified: datetime | None) -> dict:
    ref = official or provider
    out = {
        "source_delay_ms": _ms(ref, received),            # publication -> réception serveur (fournisseur + réseau)
        "provider_delay_ms": _ms(official, provider),     # publication officielle -> fournisseur
        "internal_ms": _ms(received, alert),              # réception -> première alerte
        "notify_ms": _ms(alert, notified),                # alerte -> notification envoyée
        "end_to_end_ms": _ms(ref, notified or alert),
        "reference": "official" if official else ("provider" if provider else None),
    }
    vals = [v for k, v in out.items() if k.endswith("_ms") and v is not None]
    out["clock_skew_suspect"] = any(v < -1000 for v in vals)   # valeur négative : horloge locale décalée
    return out


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def summarize(rows: list[dict]) -> dict:
    out: dict = {"n": len(rows)}
    for key in ("source_delay_ms", "internal_ms", "notify_ms", "end_to_end_ms"):
        vals = [r[key] for r in rows if r.get(key) is not None]
        out[key] = {"n": len(vals), "p50": percentile(vals, 50), "p95": percentile(vals, 95), "max": max(vals) if vals else None}
    return out

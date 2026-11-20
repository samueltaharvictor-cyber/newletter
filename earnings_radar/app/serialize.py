"""Modèles de lecture JSON (API REST et WebSocket) : toutes les heures sont fournies en UTC + ET + Paris."""
from __future__ import annotations

from ..core.domain import EarningsEvent, EventStatus
from ..core.latency import compute_latencies
from ..core.marketcal import classify_release, label_window, tri_time

SOURCE_LABELS = {"sec_edgar_8k": "Dépôt officiel SEC (8-K)", "simulated": "Événement de test"}


def _iso(d):
    return d.isoformat() if d else None


def event_to_api(ev: EarningsEvent) -> dict:
    w = label_window(ev.report_date, ev.time_label)
    return {
        "id": ev.id, "ticker": ev.ticker, "company": ev.company_name, "report_date": ev.report_date.isoformat(),
        "time_label": ev.time_label.value, "scheduled_at": _iso(ev.scheduled_at), "times": tri_time(ev.scheduled_at),
        "window": {"start": w[0].isoformat(), "end": w[1].isoformat()} if w else None,
        "date_confirmed": ev.date_confirmed, "time_confirmed": ev.time_confirmed,
        "confirmation": "CONFLICT" if ev.conflict else ("CONFIRMED" if ev.date_confirmed else "ESTIMATED"),
        "status": ev.status.value, "eps_estimate": ev.eps_estimate, "revenue_estimate": ev.revenue_estimate,
        "fiscal_year": ev.fiscal_year, "fiscal_quarter": ev.fiscal_quarter, "sources": ev.sources,
        "first_seen_at": _iso(ev.first_seen_at), "last_changed_at": _iso(ev.last_changed_at),
        "reported_at": _iso(ev.reported_at), "detected_event_id": ev.detected_event_id, "missing_cycles": ev.missing_cycles,
        "signal_state": "EVENT_DETECTED" if ev.status == EventStatus.REPORTED else None,
    }


def detected_to_api(row: dict) -> dict:
    ref = row["published_at"] or row["provider_at"] or row["received_at"]
    return {
        "id": row["id"], "kind": row["kind"], "ticker": row["ticker"], "cik": row["cik"], "company": row["company_name"],
        "title": row["title"], "url": row["url"], "source": row["source"],
        "source_label": SOURCE_LABELS.get(row["source"], row["source"]),
        "sources": [row["source"], *row["extra_sources"]], "confidence": row["confidence"], "mode": row["detection_mode"],
        "simulated": row["detection_mode"] == "simulated", "scheduled": row["scheduled"],
        "calendar_event_id": row["calendar_event_id"], "published_at": _iso(row["published_at"]),
        "provider_at": _iso(row["provider_at"]), "received_at": _iso(row["received_at"]), "alert_at": _iso(row["alert_at"]),
        "notified_at": _iso(row["notified_at"]), "times": tri_time(ref), "payload": row["payload"], "stage": "DETECTED",
        "session": (classify_release(ref).value if classify_release(ref) else "CLOSED"),
        "latency": compute_latencies(official=row["published_at"], provider=row["provider_at"], received=row["received_at"],
                                     alert=row["alert_at"], notified=row["notified_at"]),
    }

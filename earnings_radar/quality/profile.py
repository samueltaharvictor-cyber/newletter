"""Profilage des tables (méthode du skill explore-data) : volumétrie, taux de nuls, valeurs distinctes, plages de dates.
SQL portable PostgreSQL / SQLite — sert à vérifier ce que les fournisseurs renvoient réellement (champs vides, unités)."""
from __future__ import annotations

SPEC: dict[str, dict] = {
    "earnings_observations": {"cols": ["source", "ticker", "report_date", "time_label", "scheduled_at", "date_confirmed",
                                       "eps_estimate", "revenue_estimate", "fiscal_quarter"], "dates": ["report_date", "observed_at"]},
    "earnings_events": {"cols": ["ticker", "time_label", "status", "date_confirmed", "time_confirmed", "conflict",
                                 "eps_estimate", "revenue_estimate", "reported_at"], "dates": ["report_date", "first_seen_at"]},
    "earnings_revisions": {"cols": ["kind", "ticker"], "dates": ["changed_at"]},
    "consensus_snapshots": {"cols": ["ticker", "source", "eps_estimate", "revenue_estimate"], "dates": ["captured_at"]},
    "detected_events": {"cols": ["source", "ticker", "confidence", "detection_mode", "scheduled", "published_at", "notified_at"],
                        "dates": ["received_at"]},
    "alerts": {"cols": ["stage", "ticker"], "dates": ["created_at"]},
}


async def profile_database(db) -> dict:
    out: dict = {}
    for table, spec in SPEC.items():
        n = (await db.fetchone(f"SELECT COUNT(*) AS n FROM {table}"))["n"]
        cols = {}
        for c in spec["cols"]:
            r = await db.fetchone(f"SELECT COUNT(*) - COUNT({c}) AS nulls, COUNT(DISTINCT {c}) AS distinct_n FROM {table}")
            cols[c] = {"null_rate": (r["nulls"] / n) if n else None, "distinct": r["distinct_n"]}
        ranges = {}
        for c in spec["dates"]:
            r = await db.fetchone(f"SELECT MIN({c}) AS lo, MAX({c}) AS hi FROM {table}")
            ranges[c] = {"min": str(r["lo"]) if r["lo"] is not None else None, "max": str(r["hi"]) if r["hi"] is not None else None}
        out[table] = {"rows": n, "columns": cols, "date_ranges": ranges}
    return out


def format_profile(p: dict) -> str:
    lines = []
    for table, t in p.items():
        lines.append(f"\n== {table} — {t['rows']} lignes")
        for c, v in t["columns"].items():
            rate = "—" if v["null_rate"] is None else f"{v['null_rate'] * 100:5.1f} %"
            lines.append(f"   {c:<20} nuls {rate:>8}   distincts {v['distinct']}")
        for c, r in t["date_ranges"].items():
            lines.append(f"   {c:<20} de {r['min']}  à {r['max']}")
    return "\n".join(lines)

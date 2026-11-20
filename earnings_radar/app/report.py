"""Rapport HTML autonome (sans serveur, sans CDN) : cartes KPI, tableau filtrable, graphiques SVG intégrés.
Méthodes des skills build-dashboard / create-viz : lisible hors ligne (iPad), mêmes chiffres que l'API, clair/sombre automatique."""
from __future__ import annotations

from collections import Counter
from html import escape

CSS = """:root{--bg:#fff;--fg:#14181f;--mut:#667085;--card:#f4f6f9;--line:#d9dee7;--acc:#2563eb;--ok:#16794c;--warn:#b54708;--bad:#b42318}
@media(prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--mut:#8b98a8;--card:#161b22;--line:#2a313c;--acc:#58a6ff;--ok:#3fb950;--warn:#d29922;--bad:#f85149}}
*{box-sizing:border-box}body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;max-width:1100px;margin:auto}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:22px 0 8px}.mut{color:var(--mut)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}.kpi{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px}
.kpi b{display:block;font-size:22px}.wrap{overflow-x:auto}table{border-collapse:collapse;width:100%}th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{color:var(--mut);font-weight:600}input{padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);width:100%;max-width:260px}
.tag{padding:1px 6px;border-radius:10px;border:1px solid var(--line);font-size:12px}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
svg text{fill:var(--mut);font:11px system-ui}svg .bar{fill:var(--acc)}"""


def _fmt(v, nd=2):
    return "—" if v is None else (f"{v / 1e6:,.1f} M" if abs(v) >= 1e6 else f"{v:.{nd}f}")


def _bars(title: str, items: list[tuple[str, float]], unit: str = "") -> str:
    if not items:
        return f"<p class='mut'>{escape(title)} : pas de données.</p>"
    w, h, pad = 520, 150, 22
    top = max(v for _, v in items) or 1
    bw = (w - 2 * pad) / len(items)
    out = [f"<svg viewBox='0 0 {w} {h}' width='100%' role='img' aria-label='{escape(title)}'>"]
    for i, (label, v) in enumerate(items):
        bh = (h - 2 * pad) * v / top
        x = pad + i * bw
        out.append(f"<rect class='bar' x='{x + 2:.1f}' y='{h - pad - bh:.1f}' width='{max(bw - 4, 1):.1f}' height='{bh:.1f}' rx='2'/>"
                   f"<text x='{x + bw / 2:.1f}' y='{h - 8}' text-anchor='middle'>{escape(label)}</text>"
                   f"<text x='{x + bw / 2:.1f}' y='{h - pad - bh - 4:.1f}' text-anchor='middle'>{v:.0f}{unit}</text>")
    out.append("</svg>")
    return f"<h3 class='mut' style='font-size:13px'>{escape(title)}</h3>" + "".join(out)


def render_report(status: dict, today: list[dict], upcoming: list[dict], detected: list[dict], analytics: dict) -> str:
    cal, det = status["calendar"], status["detection"]
    kpi = lambda label, val: f"<div class='kpi'><b>{escape(str(val))}</b><span class='mut'>{escape(label)}</span></div>"
    cov = analytics["coverage"]
    kpis = "".join([
        kpi("Session", status["market"]["session"]), kpi("Publications aujourd'hui", len(today)),
        kpi("Détectées (7 j)", det["latency"]["n"]),
        kpi("Couverture calendrier (30 j)", "—" if cov["rate"] is None else f"{cov['rate'] * 100:.0f} %"),
        kpi("Latence interne p95", "—" if det["latency"]["internal_ms"]["p95"] is None else f"{det['latency']['internal_ms']['p95']:.0f} ms"),
        kpi("Dates confirmées / estimées", f"{cal['counts']['confirmed']} / {cal['counts']['estimated']}")])
    rows = []
    for e in today:
        t = e["times"]["paris"][11:16] if e["times"] else "—"
        conf = {"CONFIRMED": "<span class='ok'>confirmée</span>", "CONFLICT": "<span class='bad'>conflit</span>"}.get(
            e["confirmation"], "<span class='warn'>estimée</span>")
        rows.append(f"<tr><td><b>{escape(e['ticker'])}</b></td><td>{escape(e['company'] or '')}</td><td>{escape(e['time_label'])}</td>"
                    f"<td>{t}</td><td>{conf}</td><td>{_fmt(e['eps_estimate'])}</td><td>{_fmt(e['revenue_estimate'])}</td>"
                    f"<td>{escape(e['status'])}</td></tr>")
    drows = "".join(
        f"<tr><td>{escape((d['times'] or {}).get('paris', '')[:19])}</td><td><b>{escape(d['ticker'] or '?')}</b></td>"
        f"<td>{escape(d['source_label'])}</td><td>{'prévu' if d['scheduled'] else '<span class=warn>non prévu</span>'}</td>"
        f"<td>{'—' if d['latency']['internal_ms'] is None else str(round(d['latency']['internal_ms'])) + ' ms'}</td></tr>" for d in detected)
    rows_html = ''.join(rows) or "<tr><td colspan='8' class='mut'>Aucune publication connue aujourd'hui.</td></tr>"
    drows_html = drows or "<tr><td colspan='5' class='mut'>Aucune détection.</td></tr>"
    per_day = Counter(e["report_date"] for e in upcoming)
    chart1 = _bars("Publications attendues par jour", [(d[5:], n) for d, n in sorted(per_day.items())[:14]])
    lat = det["latency"]
    chart2 = _bars("Latences p95 (ms)", [(k.replace("_ms", ""), lat[k]["p95"]) for k in ("source_delay_ms", "internal_ms", "notify_ms")
                                         if lat[k]["p95"] is not None], "")
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Earnings Radar — rapport</title><style>{CSS}</style></head><body>
<h1>Earnings Radar — rapport du jour</h1>
<p class="mut">Généré le {escape(status['now']['paris'])} ({escape(status['now']['et'])}) — données issues de votre serveur, aucune garantie de performance ; outil d'aide à la décision.</p>
<div class="kpis">{kpis}</div>
<h2>Publications d'aujourd'hui</h2><input id="q" placeholder="Filtrer (ticker, BMO, AMC…)" oninput="f(this.value)">
<div class="wrap"><table id="t"><thead><tr><th>Ticker</th><th>Société</th><th>Créneau</th><th>Heure Paris</th><th>Date</th><th>BPA att.</th><th>CA att.</th><th>Statut</th></tr></thead>
<tbody>{rows_html}</tbody></table></div>
<h2>Détections récentes</h2><div class="wrap"><table><thead><tr><th>Heure Paris</th><th>Ticker</th><th>Source</th><th>Calendrier</th><th>Latence interne</th></tr></thead>
<tbody>{drows_html}</tbody></table></div>
<h2>Graphiques</h2>{chart1}{chart2}
<script>function f(v){{v=v.toLowerCase();document.querySelectorAll('#t tbody tr').forEach(r=>r.style.display=r.textContent.toLowerCase().includes(v)?'':'none')}}</script>
</body></html>"""

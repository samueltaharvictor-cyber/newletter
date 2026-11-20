"""Sans argument : lance le journal. Autres commandes : check (vérifie les sources en vrai) · profile · report."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import webbrowser
from datetime import timedelta

from .config import Settings, load_env_file
from .core.marketcal import et_date

CONFIG = "config.txt"


def ensure_identity(path: str = CONFIG) -> None:
    """La SEC exige une identification (nom + e-mail) pour lire ses dépôts : demandée une seule fois, mémorisée dans config.txt."""
    if load_env_file(path).get("SEC_USER_AGENT") or os.environ.get("SEC_USER_AGENT"):
        return
    if not sys.stdin or not sys.stdin.isatty():
        return
    print("\nPremière utilisation. La SEC (l'autorité boursière américaine) demande qu'on s'identifie pour lire ses dépôts.")
    name, email = input("Ton prénom et nom : ").strip(), input("Ton adresse e-mail : ").strip()
    if name and email:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\nSEC_USER_AGENT={name} {email}\n")
        print("C'est enregistré dans config.txt (tu pourras le modifier).\n")


async def cmd_newsletter(s: Settings, loop: int, test: bool = False) -> int:
    from .newsletter.run import build
    if not s.sec_user_agent:
        print("Identification SEC manquante (SEC_USER_AGENT).")
        return 1
    nl = build(s)
    if test:
        n = await nl.send_test()
        print(f"Notification de test envoyée sur {n} canal(aux)." if n else "Aucun canal de notification n'a répondu (NTFY_URL ?).")
        return 0 if n else 1
    while True:
        sent = await nl.run_once()
        print(f"{len(sent)} résultat(s) en hausse annoncé(s).")
        if not loop:
            return 0
        await asyncio.sleep(loop)


async def cmd_run(s: Settings) -> int:
    import logging
    from .app.runtime import Runtime
    from .app.server import WebServer
    logging.basicConfig(level=s.log_level, format="%(asctime)s %(levelname)s %(message)s")
    rt = await Runtime(s).start()
    srv = WebServer(rt, s.host, s.port, s.static_dir)
    await srv.start()
    url = f"http://localhost:{srv.port}"
    print(f"\nLe Journal des Résultats US est ouvert : {url}\nLaisse cette fenêtre ouverte. Pour arrêter : Ctrl+C ou ferme la fenêtre.\n")
    if s.open_browser:
        webbrowser.open(url)
    try:
        await asyncio.Event().wait()
    finally:
        await srv.stop()
        await rt.stop()
    return 0


async def _timed(label: str, coro) -> bool:
    t0 = time.perf_counter()
    try:
        detail = await coro
        print(f"  OK    {label:<32} {(time.perf_counter() - t0) * 1000:6.0f} ms  {detail or ''}")
        return True
    except Exception as ex:   # noqa: BLE001
        print(f"  ÉCHEC {label:<32} {type(ex).__name__}: {str(ex)[:140]}")
        return False


async def cmd_check(s: Settings) -> int:
    from .app.runtime import Runtime
    rt = await Runtime(s).build()
    today, ok = et_date(rt.clock.now()), []
    print("Vérification des sources (appels réels) :")
    if rt.edgar_client:
        async def feed():
            e = await rt.edgar_client.current_8k(count=40)
            return f"{len(e)} dépôts 8-K récents"
        async def subs():
            sub = await rt.edgar_client.submissions("320193")
            return f"{sub.get('name')} : champ « items » présent = {'items' in sub['filings']['recent']}"
        async def tick():
            return f"{len(await rt.ref_client.tickers_exchange())} sociétés"
        ok += [await _timed("SEC : dépôts 8-K", feed()), await _timed("SEC : historique d'une société", subs()),
               await _timed("SEC : liste des sociétés", tick())]
    else:
        print("  --    SEC : identification manquante (config.txt)")
    for p in rt.calendar.providers:
        async def cal(p=p):
            obs = await p.fetch(today, today + timedelta(days=7))
            return (f"{len(obs)} résultats attendus sur 7 j ; {sum(1 for o in obs if o.time_label.value != 'UNKNOWN')} avec créneau "
                    f"avant/pendant/après séance ; {sum(1 for o in obs if o.eps_estimate is not None)} avec BPA attendu")
        ok.append(await _timed(f"Calendrier {p.name}", cal()))
    if not rt.calendar.providers:
        print("  --    Calendrier : aucune clé gratuite (FINNHUB_API_KEY ou FMP_API_KEY dans config.txt)")
    await rt.stop()
    print(f"\n{sum(ok)}/{len(ok)} vérifications réussies.")
    return 0 if all(ok) else 1


async def cmd_profile(s: Settings) -> int:
    from .db.core import open_database
    from .quality.profile import format_profile, profile_database
    db = await open_database(s.database_url)
    print(format_profile(await profile_database(db)))
    await db.close()
    return 0


async def cmd_report(s: Settings, out: str) -> int:
    from .app.report import render_report
    from .app.runtime import Runtime
    rt = await Runtime(s).start(background=False)
    today, svc = et_date(rt.clock.now()), rt.service
    html = render_report(await svc.status(), await svc.earnings_today(), await svc.earnings(today, today + timedelta(days=14)),
                         await svc.events(30), await svc.analytics())
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Rapport écrit : {out}")
    await rt.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="earnings_radar")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run"), sub.add_parser("check"), sub.add_parser("profile")
    sub.add_parser("report").add_argument("-o", "--out", default="rapport.html")
    sub.add_parser("newsletter").add_argument("--loop", type=int, default=0, help="relancer toutes les N secondes (sinon une seule passe)")
    sub.choices["newsletter"].add_argument("--test", action="store_true", help="envoie une notification de test")
    a = ap.parse_args(argv)
    cmd = a.cmd or "run"
    if cmd == "run":
        ensure_identity()
    s = Settings.load(CONFIG)
    jobs = {"run": lambda: cmd_run(s), "check": lambda: cmd_check(s), "profile": lambda: cmd_profile(s),
            "report": lambda: cmd_report(s, getattr(a, "out", "rapport.html")),
            "newsletter": lambda: cmd_newsletter(s, getattr(a, "loop", 0), getattr(a, "test", False))}
    try:
        return asyncio.run(jobs[cmd]())
    except KeyboardInterrupt:
        print("\nArrêt.")
        return 0


if __name__ == "__main__":
    sys.exit(main())

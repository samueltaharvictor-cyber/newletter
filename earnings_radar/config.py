"""Réglages : fichier config.txt (créé au premier lancement) + variables d'environnement. Aucune clé ne part jamais vers le navigateur."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _csv(v: str | None, default: tuple = ()) -> tuple:
    return tuple(x.strip() for x in v.split(",") if x.strip()) if v else default


def _bool(v: str | None, default: bool) -> bool:
    return default if v is None or v == "" else v.strip().lower() in ("1", "true", "yes", "on", "oui")


def load_env_file(path: str) -> dict[str, str]:
    """Lignes CLE=valeur ; # = commentaire. Fichier absent = vide."""
    out: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./data/radar.db"
    sec_user_agent: str = ""
    finnhub_api_key: str = ""
    fmp_api_key: str = ""
    fmp_daily_quota: int = 200
    calendar_priority: tuple = ("finnhub", "fmp")
    calendar_lookahead_days: int = 14
    refresh_idle_minutes: int = 60
    refresh_active_minutes: int = 15
    refresh_hot_minutes: int = 5
    universe_exchanges: tuple = ("Nasdaq", "NYSE")
    edgar_poll_seconds: float = 5.0
    edgar_backfill_hours: float = 3.0
    edgar_recovery_pages: int = 3
    ntfy_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    access_token: str = ""
    host: str = "127.0.0.1"
    port: int = 8765
    open_browser: bool = True
    enable_test_injection: bool = True
    watchdog_stale_seconds: int = 180
    static_dir: str = "web"
    min_growth_pct: float = 25.0
    state_file: str = "newsletter_state.json"
    journal_file: str = "journal.md"
    newsletter_backfill_hours: float = 2.0
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Settings":
        e = env if env is not None else os.environ
        d, g = cls(), e.get
        return cls(
            database_url=g("DATABASE_URL") or d.database_url,
            sec_user_agent=g("SEC_USER_AGENT", ""),
            finnhub_api_key=g("FINNHUB_API_KEY", ""),
            fmp_api_key=g("FMP_API_KEY", ""),
            fmp_daily_quota=int(g("FMP_DAILY_QUOTA") or d.fmp_daily_quota),
            calendar_priority=_csv(g("CALENDAR_PRIORITY"), d.calendar_priority),
            calendar_lookahead_days=int(g("CALENDAR_LOOKAHEAD_DAYS") or d.calendar_lookahead_days),
            refresh_idle_minutes=int(g("REFRESH_IDLE_MINUTES") or d.refresh_idle_minutes),
            refresh_active_minutes=int(g("REFRESH_ACTIVE_MINUTES") or d.refresh_active_minutes),
            refresh_hot_minutes=int(g("REFRESH_HOT_MINUTES") or d.refresh_hot_minutes),
            universe_exchanges=_csv(g("UNIVERSE_EXCHANGES"), d.universe_exchanges),
            edgar_poll_seconds=float(g("EDGAR_POLL_SECONDS") or d.edgar_poll_seconds),
            edgar_backfill_hours=float(g("EDGAR_BACKFILL_HOURS") or d.edgar_backfill_hours),
            edgar_recovery_pages=int(g("EDGAR_RECOVERY_PAGES") or d.edgar_recovery_pages),
            ntfy_url=g("NTFY_URL", ""), telegram_bot_token=g("TELEGRAM_BOT_TOKEN", ""), telegram_chat_id=g("TELEGRAM_CHAT_ID", ""),
            access_token=g("ACCESS_TOKEN", ""), host=g("HOST") or d.host, port=int(g("PORT") or d.port),
            open_browser=_bool(g("OPEN_BROWSER"), True), enable_test_injection=_bool(g("ENABLE_TEST_INJECTION"), True),
            watchdog_stale_seconds=int(g("WATCHDOG_STALE_SECONDS") or d.watchdog_stale_seconds),
            static_dir=g("STATIC_DIR") or d.static_dir, min_growth_pct=float(g("MIN_GROWTH_PCT") or d.min_growth_pct),
            state_file=g("STATE_FILE") or d.state_file, journal_file=g("JOURNAL_FILE") or d.journal_file,
            newsletter_backfill_hours=float(g("NEWSLETTER_BACKFILL_HOURS") or d.newsletter_backfill_hours), log_level=(g("LOG_LEVEL") or d.log_level).upper())

    @classmethod
    def load(cls, path: str = "config.txt") -> "Settings":
        """config.txt, puis variables d'environnement par-dessus."""
        return cls.from_env({**load_env_file(path), **{k: v for k, v in os.environ.items() if v != ""}})

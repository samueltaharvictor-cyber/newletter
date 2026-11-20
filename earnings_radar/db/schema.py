"""Schéma versionné (migrations appliquées au démarrage). SQL portable PostgreSQL / SQLite."""
from __future__ import annotations

_TYPES = {
    "postgres": {"{ID}": "BIGSERIAL PRIMARY KEY", "{TS}": "TIMESTAMPTZ", "{DATE}": "DATE", "{BOOL}": "BOOLEAN", "{REAL}": "DOUBLE PRECISION"},
    "sqlite": {"{ID}": "INTEGER PRIMARY KEY AUTOINCREMENT", "{TS}": "TEXT", "{DATE}": "TEXT", "{BOOL}": "INTEGER", "{REAL}": "REAL"},
}


def render(sql: str, dialect: str) -> str:
    for token, sql_type in _TYPES[dialect].items():
        sql = sql.replace(token, sql_type)
    return sql


def split_statements(script: str) -> list[str]:
    return [s.strip() for s in script.split(";") if s.strip()]


SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS earnings_observations (
  source TEXT NOT NULL, ticker TEXT NOT NULL, report_date {DATE} NOT NULL,
  time_label TEXT NOT NULL, scheduled_at {TS}, date_confirmed {BOOL},
  company_name TEXT, fiscal_year INTEGER, fiscal_quarter INTEGER,
  eps_estimate {REAL}, revenue_estimate {REAL}, observed_at {TS} NOT NULL,
  PRIMARY KEY (source, ticker, report_date)
);
CREATE TABLE IF NOT EXISTS earnings_events (
  id {ID}, ticker TEXT NOT NULL, company_name TEXT, report_date {DATE} NOT NULL,
  time_label TEXT NOT NULL, scheduled_at {TS}, date_confirmed {BOOL} NOT NULL, time_confirmed {BOOL} NOT NULL,
  conflict {BOOL} NOT NULL, status TEXT NOT NULL, sources TEXT NOT NULL,
  fiscal_year INTEGER, fiscal_quarter INTEGER, eps_estimate {REAL}, revenue_estimate {REAL},
  missing_cycles INTEGER NOT NULL DEFAULT 0, first_seen_at {TS} NOT NULL, last_seen_at {TS} NOT NULL,
  last_changed_at {TS} NOT NULL, reported_at {TS}, detected_event_id BIGINT
);
CREATE INDEX IF NOT EXISTS ix_events_ticker_status ON earnings_events (ticker, status);
CREATE INDEX IF NOT EXISTS ix_events_report_date ON earnings_events (report_date);
CREATE TABLE IF NOT EXISTS earnings_revisions (
  id {ID}, event_id BIGINT NOT NULL, ticker TEXT NOT NULL, kind TEXT NOT NULL,
  field_name TEXT, old_value TEXT, new_value TEXT, changed_at {TS} NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_revisions_changed ON earnings_revisions (changed_at);
CREATE TABLE IF NOT EXISTS consensus_snapshots (
  id {ID}, ticker TEXT NOT NULL, report_date {DATE} NOT NULL, source TEXT NOT NULL,
  eps_estimate {REAL}, revenue_estimate {REAL}, captured_at {TS} NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_consensus_key ON consensus_snapshots (ticker, report_date, source);
CREATE TABLE IF NOT EXISTS detected_events (
  id {ID}, kind TEXT NOT NULL, ticker TEXT, cik TEXT, company_name TEXT, title TEXT NOT NULL, url TEXT,
  source TEXT NOT NULL, external_id TEXT, confidence TEXT NOT NULL, detection_mode TEXT NOT NULL,
  scheduled {BOOL} NOT NULL, calendar_event_id BIGINT, published_at {TS}, provider_at {TS}, received_at {TS} NOT NULL,
  alert_at {TS}, notified_at {TS}, payload TEXT NOT NULL, extra_sources TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_detected_source_ext ON detected_events (source, external_id);
CREATE INDEX IF NOT EXISTS ix_detected_ticker ON detected_events (ticker, received_at);
CREATE TABLE IF NOT EXISTS monitor_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at {TS} NOT NULL);
CREATE TABLE IF NOT EXISTS alerts (
  id {ID}, dedup_key TEXT NOT NULL, stage TEXT NOT NULL, event_id BIGINT, ticker TEXT,
  title TEXT NOT NULL, body TEXT NOT NULL, channels TEXT NOT NULL, created_at {TS} NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_alerts_dedup ON alerts (dedup_key)
"""

SCHEMA_V2 = "CREATE TABLE IF NOT EXISTS follows (ticker TEXT PRIMARY KEY, added_at {TS} NOT NULL)"

MIGRATIONS = [(1, "init", SCHEMA_V1), (2, "follows", SCHEMA_V2)]

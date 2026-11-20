"""Accès base : SQLite (fichier local).
SQL portable : paramètres nommés `:nom`, horodatages UTC, JSON stocké en texte."""
from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import date, datetime, timezone
from typing import Any, Protocol

from .schema import MIGRATIONS, render, split_statements

class Database(Protocol):
    dialect: str
    async def execute(self, sql: str, params: dict | None = None) -> None: ...
    async def fetch(self, sql: str, params: dict | None = None) -> list[dict]: ...
    async def fetchone(self, sql: str, params: dict | None = None) -> dict | None: ...
    async def insert(self, sql: str, params: dict | None = None) -> int | None: ...   # SQL se terminant par RETURNING id
    async def close(self) -> None: ...


def _adapt(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, bool):
        return int(v)
    return v


class SqliteDatabase:
    dialect = "sqlite"

    def __init__(self, path: str = ":memory:"):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = asyncio.Lock()

    async def _run(self, fn):
        async with self._lock:
            return await asyncio.to_thread(fn)

    @staticmethod
    def _p(params: dict | None) -> dict:
        return {k: _adapt(v) for k, v in (params or {}).items()}

    async def execute(self, sql, params=None):
        p = self._p(params)
        await self._run(lambda: self._conn.execute(sql, p))

    async def fetch(self, sql, params=None):
        p = self._p(params)
        rows = await self._run(lambda: self._conn.execute(sql, p).fetchall())
        return [dict(r) for r in rows]

    async def fetchone(self, sql, params=None):
        p = self._p(params)
        row = await self._run(lambda: self._conn.execute(sql, p).fetchone())
        return dict(row) if row else None

    async def insert(self, sql, params=None):
        p = self._p(params)
        row = await self._run(lambda: self._conn.execute(sql, p).fetchone())
        return row[0] if row else None

    async def close(self):
        self._conn.close()


async def migrate(db: Database) -> list[int]:
    await db.execute(render("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, "
                            "name TEXT NOT NULL, applied_at {TS} NOT NULL)", db.dialect))
    done = {r["version"] for r in await db.fetch("SELECT version FROM schema_migrations")}
    applied = []
    for version, name, script in MIGRATIONS:
        if version in done:
            continue
        for stmt in split_statements(render(script, db.dialect)):
            await db.execute(stmt)
        await db.execute("INSERT INTO schema_migrations (version, name, applied_at) VALUES (:v, :n, :t)",
                         {"v": version, "n": name, "t": datetime.now(timezone.utc)})
        applied.append(version)
    return applied


async def open_database(url: str) -> Database:
    """SQLite uniquement (fichier local, aucune installation) : sqlite:///chemin ou sqlite:///:memory:"""
    if not url.startswith("sqlite"):
        raise ValueError("Cette édition personnelle n'utilise que SQLite (DATABASE_URL=sqlite:///./data/radar.db)")
    db: Database = SqliteDatabase(url.split("sqlite:///", 1)[1] if "sqlite:///" in url else ":memory:")
    await migrate(db)
    return db

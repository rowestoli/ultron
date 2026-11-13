"""Shared memory for every agent: one SQLite file, plain tables."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_spend (
    id INTEGER PRIMARY KEY, ts TEXT, team TEXT, agent TEXT, model TEXT,
    input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL, simulated INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY, ts TEXT, team TEXT, kind TEXT, amount_usd REAL, note TEXT
);
CREATE TABLE IF NOT EXISTS revenue (
    id INTEGER PRIMARY KEY, ts TEXT, team TEXT, source TEXT, amount_usd REAL, note TEXT, ref TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS niches (
    id INTEGER PRIMARY KEY, ts TEXT, team TEXT, keyword TEXT, product_type TEXT, rationale TEXT,
    competing_listings INTEGER, avg_favorites REAL, score REAL, verified INTEGER DEFAULT 0,
    status TEXT DEFAULT 'candidate'   -- candidate | designing | live | scale | killed
);
CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY, ts TEXT, team TEXT, niche_id INTEGER, kind TEXT, title TEXT,
    file_path TEXT, preview_path TEXT, spec_json TEXT
);
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY, ts TEXT, team TEXT, asset_id INTEGER, platform TEXT,
    external_id TEXT, url TEXT, title TEXT, price_usd REAL,
    status TEXT                       -- queued | draft | active | manual_pending | rejected | failed
);
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY, ts TEXT, listing_id INTEGER, views INTEGER, favorites INTEGER,
    sales INTEGER, revenue_usd REAL, source TEXT
);
CREATE TABLE IF NOT EXISTS pins (
    id INTEGER PRIMARY KEY, ts TEXT, listing_id INTEGER, external_id TEXT, title TEXT,
    image_path TEXT, status TEXT
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY, ts TEXT, team TEXT, action TEXT, summary TEXT, payload_json TEXT,
    status TEXT DEFAULT 'pending',    -- pending | approved | rejected | executed | failed
    result_json TEXT, decided_ts TEXT
);
CREATE TABLE IF NOT EXISTS playbook (
    id INTEGER PRIMARY KEY, ts TEXT, team TEXT, lesson TEXT, source TEXT
);
CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY, ts TEXT, allocation_json TEXT, results_json TEXT, memo TEXT
);
CREATE TABLE IF NOT EXISTS revisions (
    id INTEGER PRIMARY KEY, ts TEXT, approval_id INTEGER, role TEXT, text TEXT   -- role: owner | ai
);
CREATE TABLE IF NOT EXISTS chat (
    id INTEGER PRIMARY KEY, ts TEXT, role TEXT, text TEXT, actions_json TEXT   -- role: owner | ultron
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY, value TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def insert(self, table: str, **values) -> int:
        values.setdefault("ts", now())
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        cur = self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(values.values()))
        self.conn.commit()
        return cur.lastrowid

    def update(self, table: str, row_id: int, **values) -> None:
        sets = ", ".join(f"{k} = ?" for k in values)
        self.conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?", [*values.values(), row_id])
        self.conn.commit()

    def all(self, sql: str, *args) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, *args) -> sqlite3.Row | None:
        return self.conn.execute(sql, args).fetchone()

    def scalar(self, sql: str, *args, default=0):
        row = self.conn.execute(sql, args).fetchone()
        return default if row is None or row[0] is None else row[0]

    # --- convenience ---------------------------------------------------------

    def lessons(self, team: str, limit: int = 15) -> list[str]:
        rows = self.all("SELECT lesson FROM playbook WHERE team IN (?, 'ultron') ORDER BY id DESC LIMIT ?", team, limit)
        return [r["lesson"] for r in rows]

    def add_lesson(self, team: str, lesson: str, source: str = "analyst") -> None:
        exists = self.one("SELECT 1 FROM playbook WHERE team = ? AND lesson = ?", team, lesson)
        if not exists:
            self.insert("playbook", team=team, lesson=lesson, source=source)

    def get_kv(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM kv WHERE key = ?", key)
        return row["value"] if row else default

    def set_kv(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
        self.conn.commit()

    def latest_metrics(self, listing_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM metrics WHERE listing_id = ? ORDER BY id DESC LIMIT 1", listing_id)

    @staticmethod
    def dumps(obj) -> str:
        return json.dumps(obj, ensure_ascii=False)

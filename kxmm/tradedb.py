"""Trade ledger: fills, events (errors, halts, reconciles), drill results."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .db import iso_ts

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fills(fill_id TEXT PRIMARY KEY, ts REAL, ticker TEXT,
  book_side TEXT, yes_price REAL, count REAL, fee REAL, order_id TEXT);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts REAL, kind TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS drills(id INTEGER PRIMARY KEY, ts REAL, name TEXT, passed INTEGER,
  detail TEXT);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS uptime(minute INTEGER PRIMARY KEY);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(_SCHEMA)
    return con


def add_fills(con, rows: list[dict]) -> list[dict]:
    new = []
    for f in rows:
        cur = con.execute(
            "INSERT OR IGNORE INTO fills VALUES(?,?,?,?,?,?,?,?)",
            (f["fill_id"], iso_ts(f.get("created_time")), f["ticker"], f["book_side"],
             float(f["yes_price_dollars"]), float(f["count_fp"]), float(f.get("fee_cost") or 0),
             f.get("order_id")))
        if cur.rowcount:
            new.append(f)
    con.commit()
    return new


def event(con, ts: float, kind: str, detail: str = "") -> None:
    con.execute("INSERT INTO events(ts, kind, detail) VALUES(?,?,?)", (ts, kind, detail))
    con.commit()


def drill_result(con, ts: float, name: str, passed: bool, detail: str) -> None:
    con.execute("INSERT INTO drills(ts, name, passed, detail) VALUES(?,?,?,?)",
                (ts, name, int(passed), detail))
    con.commit()


def kv_get(con, k: str) -> str | None:
    r = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r[0] if r else None


def kv_set(con, k: str, v: str) -> None:
    con.execute("INSERT INTO kv VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
    con.commit()


def heartbeat(con, ts: float) -> None:
    """Liveness for `status`, plus one row per minute the trader was up (Gate B)."""
    con.execute("INSERT OR IGNORE INTO uptime VALUES(?)", (int(ts // 60),))
    kv_set(con, "heartbeat", repr(ts))

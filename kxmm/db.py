"""SQLite ledger (WAL). Timestamps are epoch seconds, UTC.

Books are stored only when they change; poll_state + gaps prove coverage
between stored books (Phase 1 pass rule: no gap over gap_threshold_s).
Ticker '*' in gaps means the whole loop was down.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from .book import book_digest

log = logging.getLogger("kxmm.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS programs(
  id TEXT PRIMARY KEY, market_ticker TEXT, start_ts REAL, end_ts REAL,
  period_reward INTEGER, target_size REAL, discount_bps INTEGER,
  first_seen REAL, last_seen REAL, raw TEXT);
CREATE TABLE IF NOT EXISTS markets(
  ticker TEXT PRIMARY KEY, event_ticker TEXT, series_ticker TEXT, category TEXT,
  fee_type TEXT, close_ts REAL, status TEXT, price_ranges TEXT, updated_ts REAL,
  raw TEXT);
CREATE TABLE IF NOT EXISTS selections(
  id INTEGER PRIMARY KEY, ts REAL, tickers TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS books(
  id INTEGER PRIMARY KEY, ticker TEXT, ts REAL, digest TEXT, raw TEXT);
CREATE INDEX IF NOT EXISTS books_ticker_ts ON books(ticker, ts);
CREATE TABLE IF NOT EXISTS trades(
  trade_id TEXT PRIMARY KEY, ticker TEXT, ts REAL, yes_price REAL, count REAL,
  taker_side TEXT);
CREATE INDEX IF NOT EXISTS trades_ticker_ts ON trades(ticker, ts);
CREATE TABLE IF NOT EXISTS poll_state(
  ticker TEXT PRIMARY KEY, first_ok REAL, last_ok REAL);
CREATE TABLE IF NOT EXISTS gaps(
  id INTEGER PRIMARY KEY, ticker TEXT, start_ts REAL, end_ts REAL);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(_SCHEMA)
    return con


def iso_ts(s: str | None) -> float | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def kv_get(con, k: str) -> str | None:
    r = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r[0] if r else None


def kv_set(con, k: str, v: str) -> None:
    con.execute("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (k, v))
    con.commit()


def upsert_programs(con, programs: list[dict], now: float) -> None:
    con.executemany(
        "INSERT INTO programs(id, market_ticker, start_ts, end_ts, period_reward,"
        " target_size, discount_bps, first_seen, last_seen, raw)"
        " VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET"
        " last_seen=excluded.last_seen, raw=excluded.raw, end_ts=excluded.end_ts,"
        " period_reward=excluded.period_reward",
        [(p["id"], p["market_ticker"], iso_ts(p.get("start_date")), iso_ts(p.get("end_date")),
          p.get("period_reward"), float(p.get("target_size_fp") or 0),
          p.get("discount_factor_bps"), now, now, json.dumps(p)) for p in programs])
    con.commit()


def series_of(event_ticker: str) -> str:
    return (event_ticker or "").split("-")[0]


def upsert_markets(con, markets: list[dict], series_by_ticker: dict[str, dict],
                   now: float) -> None:
    rows = []
    for m in markets:
        st = series_of(m.get("event_ticker", ""))
        s = series_by_ticker.get(st) or {}
        rows.append((m["ticker"], m.get("event_ticker"), st, s.get("category"),
                     s.get("fee_type"), iso_ts(m.get("close_time")), m.get("status"),
                     json.dumps(m.get("price_ranges") or []), now, json.dumps(m)))
    con.executemany(
        "INSERT INTO markets(ticker, event_ticker, series_ticker, category, fee_type,"
        " close_ts, status, price_ranges, updated_ts, raw) VALUES(?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(ticker) DO UPDATE SET event_ticker=excluded.event_ticker,"
        " series_ticker=excluded.series_ticker, category=excluded.category,"
        " fee_type=excluded.fee_type, close_ts=excluded.close_ts, status=excluded.status,"
        " price_ranges=excluded.price_ranges, updated_ts=excluded.updated_ts, raw=excluded.raw",
        rows)
    con.commit()


def log_selection(con, ts: float, tickers: list[str], detail: list[dict]) -> None:
    con.execute("INSERT INTO selections(ts, tickers, detail) VALUES(?,?,?)",
                (ts, json.dumps(tickers), json.dumps(detail)))
    con.commit()


def last_selection(con) -> list[str]:
    r = con.execute("SELECT tickers FROM selections ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(r[0]) if r else []


def store_book(con, ticker: str, ts: float, raw: dict) -> bool:
    digest = book_digest(raw)
    last = con.execute("SELECT digest FROM books WHERE ticker=? ORDER BY ts DESC LIMIT 1",
                       (ticker,)).fetchone()
    if last and last[0] == digest:
        return False
    con.execute("INSERT INTO books(ticker, ts, digest, raw) VALUES(?,?,?,?)",
                (ticker, ts, digest, json.dumps(raw)))
    con.commit()
    return True


def _trade_row(t: dict) -> tuple:
    ts = iso_ts(t["created_time"])
    if ts is None:
        raise ValueError("trade without created_time")
    return (t["trade_id"], t["ticker"], ts, float(t["yes_price_dollars"]),
            float(t["count_fp"]), t.get("taker_side"))


def store_trades(con, rows: list[dict]) -> int:
    """Insert new trades; a malformed row is skipped and counted, never fatal."""
    parsed, skipped = [], 0
    for t in rows:
        try:
            parsed.append(_trade_row(t))
        except (KeyError, TypeError, ValueError):
            skipped += 1
    if skipped:
        log.warning("trades: skipped %d malformed row(s)", skipped)
    before = con.total_changes
    con.executemany(
        "INSERT OR IGNORE INTO trades(trade_id, ticker, ts, yes_price, count, taker_side)"
        " VALUES(?,?,?,?,?,?)", parsed)
    con.commit()
    return con.total_changes - before


def _gap_then_set(con, ticker: str, ts: float, last: float | None, threshold: float) -> None:
    if last is not None and ts - last > threshold:
        con.execute("INSERT INTO gaps(ticker, start_ts, end_ts) VALUES(?,?,?)",
                    (ticker, last, ts))


def mark_poll(con, ticker: str, ts: float, gap_threshold_s: float) -> None:
    r = con.execute("SELECT last_ok FROM poll_state WHERE ticker=?", (ticker,)).fetchone()
    _gap_then_set(con, ticker, ts, r[0] if r else None, gap_threshold_s)
    con.execute("INSERT INTO poll_state(ticker, first_ok, last_ok) VALUES(?,?,?)"
                " ON CONFLICT(ticker) DO UPDATE SET last_ok=excluded.last_ok",
                (ticker, ts, ts))
    con.commit()


def last_heartbeat(con) -> float | None:
    v = kv_get(con, "heartbeat")
    return float(v) if v is not None else None


def heartbeat(con, ts: float, gap_threshold_s: float) -> None:
    _gap_then_set(con, "*", ts, last_heartbeat(con), gap_threshold_s)
    kv_set(con, "heartbeat", repr(ts))


def record_restart_gaps(con, now: float, gap_threshold_s: float,
                        tickers: list[str] | None = None) -> int:
    n = 0
    hb = last_heartbeat(con)
    if hb is not None and now - hb > gap_threshold_s:
        _gap_then_set(con, "*", now, hb, gap_threshold_s)
        n += 1
    kv_set(con, "heartbeat", repr(now))
    for r in con.execute("SELECT ticker, last_ok FROM poll_state").fetchall():
        if tickers is not None and r["ticker"] not in tickers:
            continue
        if now - r["last_ok"] > gap_threshold_s:
            _gap_then_set(con, r["ticker"], now, r["last_ok"], gap_threshold_s)
            con.execute("UPDATE poll_state SET last_ok=? WHERE ticker=?", (now, r["ticker"]))
            n += 1
    con.commit()
    return n

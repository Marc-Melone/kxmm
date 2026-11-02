"""Read the recorder's DB, read-only, as a replayable event stream per market.

Opened through a `file:` URI with mode=ro, so the sim can run on the Windows
box while the recorder keeps writing (SQLite WAL allows a concurrent reader).
"""
from __future__ import annotations

import bisect
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .book import Book
from .db import iso_ts

MAKER_FEE_TYPE = "quadratic_with_maker_fees"
_ORDER = {"gap_end": 0, "book": 1, "trade": 2, "gap_start": 3}


def open_readonly(path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


@dataclass
class Meta:
    ticker: str
    price_ranges: list[dict]
    close_ts: float
    occurrence_ts: float | None
    maker_fee: bool


@dataclass
class Program:
    start: float
    end: float
    daily_pool: float
    target: float
    discount: float


@dataclass
class Trade:
    ts: float
    yes_price: float
    count: float
    taker_side: str


@dataclass
class Event:
    ts: float
    kind: str
    book: Book | None = None
    trade: Trade | None = None


def market_meta(con, ticker: str) -> Meta:
    r = con.execute("SELECT * FROM markets WHERE ticker=?", (ticker,)).fetchone()
    if r is None:
        raise KeyError(f"no market metadata for {ticker}")
    raw = json.loads(r["raw"] or "{}")
    return Meta(ticker=ticker, price_ranges=json.loads(r["price_ranges"] or "[]"),
                close_ts=r["close_ts"], occurrence_ts=iso_ts(raw.get("occurrence_datetime")),
                maker_fee=r["fee_type"] == MAKER_FEE_TYPE)


def programs_for(con, ticker: str) -> list[Program]:
    out = []
    for r in con.execute("SELECT * FROM programs WHERE market_ticker=?", (ticker,)):
        days = max((r["end_ts"] - r["start_ts"]) / 86400.0, 1 / 24)
        out.append(Program(start=r["start_ts"], end=r["end_ts"],
                           daily_pool=r["period_reward"] / 10_000 / days,
                           target=r["target_size"], discount=(r["discount_bps"] or 0) / 10_000))
    return out


def program_at(programs: list[Program], ts: float) -> Program | None:
    live = [p for p in programs if p.start <= ts < p.end]
    return max(live, key=lambda p: p.daily_pool) if live else None


def events(con, ticker: str, t0: float, t1: float) -> list[Event]:
    out = [Event(r["ts"], "book", book=Book.from_orderbook(json.loads(r["raw"])))
           for r in con.execute("SELECT ts, raw FROM books WHERE ticker=? AND ts BETWEEN ? AND ?",
                                (ticker, t0, t1))]
    out += [Event(r["ts"], "trade", trade=Trade(r["ts"], r["yes_price"], r["count"],
                                                r["taker_side"]))
            for r in con.execute("SELECT * FROM trades WHERE ticker=? AND ts BETWEEN ? AND ?",
                                 (ticker, t0, t1))]
    # a gap in the books, the trades (fills unknowable) or the whole loop pauses
    # replay; they nest and overlap, so they are merged first
    for start, end in merged_gaps(con, (ticker, f"trades:{ticker}", "*")):
        if end < t0 or start > t1:
            continue
        out.append(Event(max(start, t0), "gap_start"))   # a replay can start inside a gap
        if end <= t1:
            out.append(Event(end, "gap_end"))
    return sorted(out, key=lambda e: (e.ts, _ORDER[e.kind]))


def merged_gaps(con, keys: tuple[str, ...]) -> list[tuple[float, float]]:
    """Union of the recorded gaps for these gap keys, as sorted disjoint intervals."""
    marks = ",".join("?" * len(keys))
    rows = sorted((r[0], r[1]) for r in con.execute(
        f"SELECT start_ts, end_ts FROM gaps WHERE ticker IN ({marks})", keys))
    out: list[list[float]] = []
    for s, e in rows:
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


class MidSeries:
    """Mid at any time: the last book at or before it, unless inside a gap."""

    def __init__(self, con, ticker: str):
        rows = [(r["ts"], Book.from_orderbook(json.loads(r["raw"])).mid())
                for r in con.execute("SELECT ts, raw FROM books WHERE ticker=? ORDER BY ts",
                                     (ticker,))]
        self._ts = [t for t, _ in rows]
        self._mid = [m for _, m in rows]
        self._gaps = merged_gaps(con, (ticker, "*"))

    def at(self, ts: float) -> float | None:
        if any(s <= ts < e for s, e in self._gaps):
            return None
        i = bisect.bisect_right(self._ts, ts) - 1
        return self._mid[i] if i >= 0 else None


def tickers_in(con, t0: float, t1: float) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT DISTINCT ticker FROM books WHERE ts BETWEEN ? AND ? ORDER BY ticker", (t0, t1))]


def recorded_days(con, t0: float | None = None, t1: float | None = None) -> int:
    return con.execute("SELECT COUNT(DISTINCT date(ts, 'unixepoch')) FROM books"
                       " WHERE ts BETWEEN ? AND ?",
                       (t0 if t0 is not None else 0, t1 if t1 is not None else 1e12)).fetchone()[0]

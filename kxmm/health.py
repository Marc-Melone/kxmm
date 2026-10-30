"""Is the loop alive, and does Phase 1's pass rule hold?

Phase 1 passes when, over the last 24 h, at least 20 markets were polled
from the window's start to now with no gap over gap_threshold_s, and the
loop itself ('*') had no such gap.
"""
from __future__ import annotations

from . import db


def stale_after_s(cfg: dict) -> float:
    """How long a healthy loop can stay silent: one request's worst case
    (every attempt timing out, backoff between attempts) plus three book polls."""
    api = cfg["api"]
    attempts = api.get("max_attempts", 4)
    worst = api.get("timeout_s", 20) * attempts + sum(2 ** i for i in range(attempts - 1))
    return worst + 3 * cfg["record"]["book_poll_s"]


def loop_state(con, now: float, stale_after: float) -> tuple[str, float | None]:
    hb = db.last_heartbeat(con)
    if hb is None:
        return "STOPPED", None
    age = now - hb
    return ("RUNNING" if age < stale_after else "STOPPED"), age


def report(con, now: float, window_s: float = 86400, gap_threshold_s: float = 300,
           min_markets: int = 20) -> dict:
    start = now - window_s
    gaps = [dict(r) for r in con.execute(
        "SELECT ticker, start_ts, end_ts FROM gaps WHERE end_ts > ? AND start_ts < ?",
        (start, now))]
    gapped = {g["ticker"] for g in gaps}
    clean = [r["ticker"] for r in con.execute(
        "SELECT ticker FROM poll_state WHERE first_ok <= ? AND last_ok >= ? ORDER BY ticker",
        (start, now - gap_threshold_s)) if r["ticker"] not in gapped]
    loop_gaps = [g for g in gaps if g["ticker"] == "*"]
    count = lambda t: con.execute(  # noqa: E731
        f"SELECT COUNT(*) FROM {t} WHERE ts >= ?", (start,)).fetchone()[0]
    return {
        "universe": len(db.last_selection(con)),
        "books_window": count("books"),
        "trades_window": count("trades"),
        "clean_markets": clean,
        "loop_gaps": loop_gaps,
        "max_gap_s": max((g["end_ts"] - g["start_ts"] for g in gaps), default=0),
        "phase1_pass": len(clean) >= min_markets and not loop_gaps,
    }

"""Is the loop alive, and does Phase 1's pass rule hold?

Phase 1 passes when, over the last 24 h, at least 20 markets had both their
books and their trades ('trades:<ticker>') polled from the window's start
to now with no gap over gap_threshold_s, and the loop itself ('*') had no
such gap. Shorter gaps are recorded for the simulator but don't fail it.
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
    stopped = db.kv_get(con, "stopped")
    if stopped is not None and float(stopped) >= hb:       # clean exit after its last beat
        return "STOPPED", age
    return ("RUNNING" if age < stale_after else "STOPPED"), age


def report(con, now: float, window_s: float = 86400, gap_threshold_s: float = 300,
           min_markets: int = 20) -> dict:
    start = now - window_s
    gaps = [dict(r) for r in con.execute(
        "SELECT ticker, start_ts, end_ts FROM gaps WHERE end_ts > ? AND start_ts < ?",
        (start, now))]
    # every gap over coverage_gap_s is recorded; only long ones break Phase 1
    long_gaps = [g for g in gaps if g["end_ts"] - g["start_ts"] > gap_threshold_s]
    gapped = {g["ticker"] for g in long_gaps}
    covered = {r["ticker"] for r in con.execute(
        "SELECT ticker FROM poll_state WHERE first_ok <= ? AND last_ok >= ?",
        (start, now - gap_threshold_s))} - gapped
    # a clean market has both its books and its trades covered
    clean = sorted(t for t in covered
                   if not t.startswith("trades:") and f"trades:{t}" in covered)
    loop_gaps = [g for g in long_gaps if g["ticker"] == "*"]
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

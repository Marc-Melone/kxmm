"""Gate B (DESIGN.md): 5 straight clean days on demo, and every drill passed.

A clean day is a UTC day with 20+ hours of trader uptime and no 'error' or
'reconcile_bad' event. Outages (network, rate limits) are handled, not errors.
"""
from __future__ import annotations

from .drills import DRILLS

NEED_DAYS, MIN_HOURS = 5, 20


def gate_b(con, now: float, tcfg: dict) -> tuple[bool, list[str]]:
    hours: dict[int, float] = {}
    for (minute,) in con.execute("SELECT minute FROM uptime"):
        day = minute * 60 // 86400
        hours[day] = hours.get(day, 0.0) + 1 / 60
    bad = {int(ts // 86400) for (ts,) in con.execute(
        "SELECT ts FROM events WHERE kind IN ('error', 'reconcile_bad')")}

    def clean(d: int) -> bool:
        return hours.get(d, 0.0) >= MIN_HOURS and d not in bad

    today = int(now // 86400)
    d = today if clean(today) else today - 1
    run, first = 0, None
    while clean(d):
        run, first, d = run + 1, d, d - 1
    reasons = []
    if run < NEED_DAYS:
        reasons.append(f"{run} of {NEED_DAYS} clean days ({MIN_HOURS}+ h trader uptime, "
                       "no errors or reconcile mismatches)")
    since = (first if first is not None else today) * 86400
    passed = {r[0] for r in con.execute("SELECT name FROM drills WHERE passed=1 AND ts >= ?",
                                        (since,))}
    missing = [n for n in DRILLS if n not in passed]
    if missing:
        reasons.append("drills not yet passed in this window: " + ", ".join(missing))
    return not reasons, reasons

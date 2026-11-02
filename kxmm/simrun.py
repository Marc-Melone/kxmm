"""Run the simulator over a recording and print the Gate A report.

Reads the recorder DB read-only; writes out/sim.db (runs, market_days,
settlement cache) and out/sim_market_days.csv.
"""
from __future__ import annotations

import csv
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from .replay import covered_hours_by_day, open_readonly, recorded_days, tickers_in
from .sim import simulate_market
from .stats import bootstrap_lower, day_totals, fill_stats, gate_a, market_days, t_stat

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, ts REAL, strategy_version TEXT,
  params TEXT, t0 REAL, t1 REAL);
CREATE TABLE IF NOT EXISTS market_days(run_id INTEGER, ticker TEXT, day TEXT, fills INTEGER,
  trading REAL, reward_raw REAL, reward REAL, total REAL);
CREATE TABLE IF NOT EXISTS settlements(ticker TEXT PRIMARY KEY, value REAL, fetched_ts REAL);
"""
_RESULT = {"yes": 1.0, "no": 0.0}


def settlements(client, tickers: list[str], cache_con) -> dict[str, float | None]:
    """1.0 / 0.0 for settled markets, None when unknown. Only known results are cached."""
    cache_con.executescript(_SCHEMA)
    out: dict[str, float | None] = {}
    for t in tickers:
        r = cache_con.execute("SELECT value FROM settlements WHERE ticker=?", (t,)).fetchone()
        if r is not None:
            out[t] = r[0]
            continue
        value = None
        if client is not None:
            try:
                value = _RESULT.get((client.market(t) or {}).get("result") or "")
            except Exception:  # noqa: BLE001 — unknown is an answer, not a crash
                value = None
        if value is not None:
            cache_con.execute("INSERT OR REPLACE INTO settlements VALUES(?,?,?)",
                              (t, value, time.time()))
            cache_con.commit()
        out[t] = value
    return out


def _date(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _money(x: float | None) -> str:
    return "n/a" if x is None else f"${x:,.2f}"


def _day_line(label: str, values: list[float], scfg: dict) -> str:
    if len(values) < 2:
        return f"  {label:<13} {len(values)} day(s): too few to test"
    lower = bootstrap_lower(values, scfg["bootstrap_n"], scfg["bootstrap_seed"])
    t = t_stat(values)
    return (f"  {label:<13} mean {_money(sum(values) / len(values))}/day  "
            f"t={'n/a' if t is None else f'{t:.2f}'}  97.5% lower bound {_money(lower)}  "
            f"-> beyond chance: {'YES' if lower > 0 else 'no'}")


def run(cfg: dict, db_path: Path, out_dir: Path, t0: float | None, t1: float | None,
        client=None) -> str:
    scfg = cfg["sim"]
    head = f"kxmm simulation — Strategy version {scfg['strategy_version']}"
    con = open_readonly(db_path)
    lo, hi = con.execute("SELECT MIN(ts), MAX(ts) FROM books").fetchone()
    if lo is None:
        return f"{head}\nNo recorded books in {db_path}.\nGATE A: NOT YET — 0 of " \
               f"{scfg['gate_a_min_days']} days recorded"
    last_poll = con.execute("SELECT MAX(last_ok) FROM poll_state").fetchone()[0] or hi
    t0 = lo if t0 is None else t0
    t1 = max(hi, last_poll) if t1 is None else t1

    results, skipped = [], []
    for tk in tickers_in(con, t0, t1):
        try:
            results.append(simulate_market(con, tk, t0, t1, cfg))
        except KeyError:
            skipped.append(tk)                      # no market metadata recorded
    mds = market_days(results, scfg)
    days = recorded_days(con, t0, t1)
    covered = sum(1 for h in covered_hours_by_day(con, t0, t1).values()
                  if h >= scfg["gate_a_min_hours_per_day"])
    fs = fill_stats(results, mds, scfg)
    totals = list(day_totals(mds, "total").values())
    lower = (bootstrap_lower(totals, scfg["bootstrap_n"], scfg["bootstrap_seed"])
             if len(totals) >= 2 else None)
    gate = gate_a(mds, covered, fs, lower, scfg)

    out_dir.mkdir(parents=True, exist_ok=True)
    sim = sqlite3.connect(out_dir / "sim.db")
    sim.executescript(_SCHEMA)
    run_id = sim.execute("INSERT INTO runs(ts, strategy_version, params, t0, t1) VALUES(?,?,?,?,?)",
                         (time.time(), scfg["strategy_version"], json.dumps(scfg), t0, t1)).lastrowid
    sim.executemany("INSERT INTO market_days VALUES(?,?,?,?,?,?,?,?)",
                    [(run_id, m.ticker, m.day, m.fills, m.trading, m.reward_raw, m.reward, m.total)
                     for m in mds])
    sim.commit()
    with open(out_dir / "sim_market_days.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "day", "fills", "trading", "reward_raw", "reward", "total"])
        w.writerows([m.ticker, m.day, m.fills, round(m.trading, 6), round(m.reward_raw, 6),
                     round(m.reward, 6), round(m.total, 6)] for m in mds)
    settle = settlements(client, [r.ticker for r in results], sim)

    per_market: dict[str, list[float]] = {}
    for m in mds:
        acc = per_market.setdefault(m.ticker, [0.0, 0.0, 0.0, 0])
        acc[0] += m.total
        acc[1] += m.trading
        acc[2] += m.reward
        acc[3] += m.fills
    lines = [head,
             f"Data: {_date(t0)} to {_date(t1)} ({days} day(s) with books; Recorded days "
             f"(20+ h): {covered}), {len(results)} markets, "
             f"{sum(len(r.fills) for r in results)} fills"
             + (f"; {len(skipped)} skipped without metadata" if skipped else ""),
             "Per market (net, best first):"]
    for tk, (tot, trd, rew, n) in sorted(per_market.items(), key=lambda kv: -kv[1][0]):
        lines.append(f"  {tk:<40} net {_money(tot):>9}  trading {_money(trd):>9}  "
                     f"reward {_money(rew):>8}  fills {n}")
    lines += [f"Daily totals ({days} day(s), bootstrap by day):",
              _day_line("total", list(day_totals(mds, "total").values()), scfg),
              _day_line("trading only", list(day_totals(mds, "trading").values()), scfg)]
    if fs.mean is None:
        lines.append(f"Per fill ({scfg['primary_markout_s']} s markout): no fills with a markout yet")
    else:
        lines.append(f"Per fill ({scfg['primary_markout_s']} s markout return): n={fs.n}, "
                     f"mean {fs.mean:.2%}" + (f", sd {fs.sd:.2%}" if fs.sd is not None else "")
                     + f", {fs.per_market_day:.1f} fills per quoted market-day")
    fills = [f for r in results for f in r.fills]
    h0 = scfg["primary_markout_s"]
    parts = []
    for h in scfg["markouts_s"]:
        vals = [f.markouts[h] for f in fills if f.markouts.get(h) is not None]
        parts.append(f"{h} s {_money(sum(vals))} ({len(vals)} known)")
    settled = [((settle[f.ticker] - f.price) * f.size if f.side == "yes"
                else ((1 - f.price) - settle[f.ticker]) * f.size)
               for f in fills if settle.get(f.ticker) is not None]
    parts.append(f"settlement {_money(sum(settled))} ({len(settled)} known)")
    lines.append("Markouts, all fills, before fees: " + " | ".join(parts))
    unknown = [f for f in fills if f.markouts.get(h0) is None]
    lines.append(f"Unknown {h0} s markouts: {len(unknown)} of {len(fills)} fills; at the last "
                 f"known mid they total {_money(sum(f.fallback or 0.0 for f in unknown))}")
    pos = [tk for tk, v in per_market.items() if v[0] > 0]
    pos_filled = [tk for tk in pos if per_market[tk][3] > 0]
    lines.append(f"Positive markets: {len(pos)} of {len(per_market)} "
                 f"({len(pos_filled)} with at least one fill). Rewards are estimates, "
                 f"not yet checked against a real Kalshi payout.")
    hours: dict[str, float] = {}
    for r in results:
        for d, secs in r.quoted_s_by_day.items():
            hours[d] = hours.get(d, 0.0) + secs / 3600
    if hours:
        hv = sorted(hours.values())
        lines.append(f"Quoted market-hours per day: min {hv[0]:.1f}, median "
                     f"{hv[len(hv) // 2]:.1f}, max {hv[-1]:.1f}")
    known = [r for r in results if settle.get(r.ticker) is not None]
    open_ = [r for r in results if settle.get(r.ticker) is None and r.last_mid is not None]
    lines.append(
        f"Inventory luck: {len(known)} settled market(s) {_money(sum(r.cash + r.position * settle[r.ticker] for r in known))}; "
        f"{len(open_)} open at last mid {_money(sum(r.cash + r.position * r.last_mid for r in open_))}")
    lines.append(f"GATE A: {gate.verdict}" + (f" — {'; '.join(gate.reasons)}" if gate.reasons else ""))
    if fs.fills_needed and gate.live_days_needed is not None:
        lines.append(f"LIVE TARGET: {fs.fills_needed} fills ≈ {gate.live_days_needed:.0f} days "
                     f"at {scfg['live_markets']} markets")
    else:
        lines.append("LIVE TARGET: not estimable yet (needs fills with a positive mean markout)")
    return "\n".join(lines)

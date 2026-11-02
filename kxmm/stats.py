"""The pre-registered test (STRATEGY.md "Metrics and the test", "Gate A").

Primary per market-day = sum of (primary markout - fee) over fills with a
known markout, plus reward x haircut. "Beyond chance" bootstraps calendar-day
totals; Gate A counts markets with positive net over enough recorded days.
"""
from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .sim import MarketResult


@dataclass
class MarketDay:
    ticker: str
    day: str
    fills: int
    trading: float
    reward_raw: float
    reward: float
    total: float


@dataclass
class FillStats:
    n: int
    mean: float | None
    sd: float | None
    fills_needed: int | None
    per_market_day: float


@dataclass
class GateA:
    passed: bool
    markets_positive: int
    days: int
    reasons: list[str] = field(default_factory=list)
    live_days_needed: float | None = None
    verdict: str = "NOT YET"           # "PASS", "PASS — rewards only" or "NOT YET"


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def market_days(results: list[MarketResult], scfg: dict) -> list[MarketDay]:
    h, haircut = scfg["primary_markout_s"], scfg["reward_haircut"]
    out = []
    for r in results:
        by_day: dict[str, list] = {}
        for f in r.fills:
            by_day.setdefault(_day(f.ts), []).append(f)
        for day, secs in sorted(r.quoted_s_by_day.items()):
            if secs <= 0:
                continue
            fs = by_day.get(day, [])
            trading = sum(f.markouts[h] - f.fee for f in fs if f.markouts.get(h) is not None)
            raw = r.reward_by_day.get(day, 0.0)
            out.append(MarketDay(r.ticker, day, len(fs), trading, raw, raw * haircut,
                                 trading + raw * haircut))
    return out


def day_totals(mds: list[MarketDay], field_name: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for m in mds:
        out[m.day] = out.get(m.day, 0.0) + getattr(m, field_name)
    return out


def bootstrap_lower(values: list[float], n: int, seed: int, q: float = 0.025) -> float:
    rng = random.Random(seed)
    k = len(values)
    means = sorted(sum(rng.choice(values) for _ in range(k)) / k for _ in range(n))
    return means[int(q * n)]


def t_stat(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    sd = statistics.stdev(values)
    return None if sd == 0 else statistics.mean(values) / (sd / math.sqrt(len(values)))


def fill_stats(results: list[MarketResult], mds: list[MarketDay], scfg: dict) -> FillStats:
    h = scfg["primary_markout_s"]
    rets = [f.markouts[h] / (f.price * f.size) for r in results for f in r.fills
            if f.markouts.get(h) is not None and f.price * f.size > 0]
    n = len(rets)
    mean = statistics.mean(rets) if n else None
    sd = statistics.stdev(rets) if n >= 2 else None
    needed = math.ceil((2 * sd / mean) ** 2) if (mean and sd and mean > 0) else None
    quoted_days = len(mds)
    per_md = sum(len(r.fills) for r in results) / quoted_days if quoted_days else 0.0
    return FillStats(n, mean, sd, needed, per_md)


def gate_a(mds: list[MarketDay], covered_days: int, fs: FillStats,
           day_lower: float | None, scfg: dict) -> GateA:
    """STRATEGY.md 1.1.0 Gate A: enough recorded days and measured fills, a
    total beyond chance, and a positive trading edge ("rewards only" if not)."""
    by_market: dict[str, float] = {}
    for m in mds:
        by_market[m.ticker] = by_market.get(m.ticker, 0.0) + m.total
    positive = sum(1 for v in by_market.values() if v > 0)
    need_d, need_f = scfg["gate_a_min_days"], scfg["gate_a_min_fills"]
    reasons = []
    if covered_days < need_d:
        reasons.append(f"{covered_days} of {need_d} days with at least "
                       f"{scfg['gate_a_min_hours_per_day']} h recorded")
    if fs.n < need_f:
        reasons.append(f"{fs.n} of {need_f} fills with a known "
                       f"{scfg['primary_markout_s']} s markout")
    if day_lower is None or day_lower <= 0:
        reasons.append("total not beyond chance (day bootstrap lower bound "
                       + ("n/a" if day_lower is None else f"${day_lower:,.2f}") + ")")
    trading = sum(m.trading for m in mds)
    live_days = None
    if fs.fills_needed and fs.per_market_day > 0:
        live_days = fs.fills_needed / (fs.per_market_day * scfg["live_markets"])
    if reasons:
        verdict = "NOT YET"
    elif trading > 0:
        verdict = "PASS"
    else:
        verdict = "PASS — rewards only"
        reasons.append(f"trading-only P&L ${trading:,.2f}: the edge is rewards alone")
    return GateA(verdict != "NOT YET", positive, covered_days, reasons, live_days, verdict)

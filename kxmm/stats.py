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


def gate_a(mds: list[MarketDay], recorded_days: int, fs: FillStats, scfg: dict) -> GateA:
    by_market: dict[str, float] = {}
    for m in mds:
        by_market[m.ticker] = by_market.get(m.ticker, 0.0) + m.total
    positive = sum(1 for v in by_market.values() if v > 0)
    need_m, need_d = scfg["gate_a_min_markets"], scfg["gate_a_min_days"]
    reasons = []
    if recorded_days < need_d:
        reasons.append(f"{recorded_days} of {need_d} days recorded")
    if positive < need_m:
        reasons.append(f"{positive} of {need_m} markets with positive net")
    live_days = None
    if fs.fills_needed and fs.per_market_day > 0:
        live_days = fs.fills_needed / (fs.per_market_day * scfg["live_markets"])
    return GateA(not reasons, positive, recorded_days, reasons, live_days)

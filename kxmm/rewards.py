"""Kalshi liquidity-incentive scoring (DESIGN.md "Reward and rate facts").

Per side, per snapshot: the reference price is the first level, walking down
from the best bid, where cumulative size reaches target/5. Levels at or above
it score size x 1.0; below it, size x discount ** ticks_below. Only levels
that help reach the target size qualify, and a side whose total depth is
under the target scores nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class SideScore:
    ref_price: float | None
    qualifying_score: float
    depth: float
    meets_target: bool


def tick_size(price: float, price_ranges: list[dict]) -> float:
    for r in price_ranges:
        if float(r["start"]) <= price < float(r["end"]):
            return float(r["step"])
    return float(price_ranges[-1]["step"])


def side_score(levels: list[tuple[float, float]], target_size: float,
               discount: float, price_ranges: list[dict]) -> SideScore:
    depth = sum(s for _, s in levels)
    ref = None
    cum = 0.0
    for p, s in levels:
        cum += s
        if cum >= target_size / 5.0:
            ref = p
            break
    if ref is None:
        return SideScore(None, 0.0, depth, False)
    score = 0.0
    cum = 0.0
    for p, s in levels:
        ticks = 0 if p >= ref - 1e-12 else round((ref - p) / tick_size(p, price_ranges))
        score += s * discount ** ticks
        cum += s
        if cum >= target_size:
            break
    return SideScore(ref, score, depth, depth >= target_size)


def est_share(side: SideScore, our_size: float) -> float:
    if not side.meets_target:
        return 0.0
    return our_size / (side.qualifying_score + our_size)


def _ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def daily_pool_usd(program: dict) -> float:
    days = (_ts(program["end_date"]) - _ts(program["start_date"])) / 86400.0
    return program["period_reward"] / 10_000 / max(days, 1 / 24)

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


def our_side_share(levels: list[tuple[float, float]], our_price: float | None,
                   our_size: float, target_size: float, discount: float,
                   price_ranges: list[dict]) -> float:
    """Our share of one side's snapshot score, with our order added to the book."""
    if our_price is None or our_size <= 0:
        return 0.0
    merged: dict[float, float] = {}
    for p, s in levels:
        merged[p] = merged.get(p, 0.0) + s
    key = next((p for p in merged if abs(p - our_price) < 1e-9), our_price)
    merged[key] = merged.get(key, 0.0) + our_size
    combined = sorted(merged.items(), reverse=True)
    side = side_score(combined, target_size, discount, price_ranges)
    if not side.meets_target:
        return 0.0
    cum = 0.0
    for p, s in combined:                     # does our level fall within the target depth?
        if p == key:
            break
        cum += s
        if cum >= target_size:
            return 0.0
    ticks = 0 if our_price >= side.ref_price - 1e-12 else round(
        (side.ref_price - our_price) / tick_size(our_price, price_ranges))
    return our_size * discount ** ticks / side.qualifying_score

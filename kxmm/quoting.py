"""Pre-registered quoting rules, strategy v1.0.0 (STRATEGY.md "Quoting").

Each side bids at the reward reference price or further from mid, whichever
the inventory-skewed Avellaneda–Stoikov reservation (in log-odds) gives.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .book import Book
from .fair import expit, logit, microprice
from .replay import Meta, Program
from .rewards import side_score, tick_size


@dataclass
class Quotes:
    yes_bid: float | None
    yes_size: float
    no_bid: float | None
    no_size: float


def floor_tick(p: float, price_ranges: list[dict]) -> float:
    step = tick_size(p, price_ranges)
    return round(math.floor(p / step + 1e-9) * step, 6)


def blackout_at(meta: Meta, scfg: dict) -> float:
    first = min(meta.close_ts, meta.occurrence_ts or math.inf)
    return first - scfg["blackout_before_s"]


def make_quotes(book: Book, position: float, var_per_s: float | None, now: float,
                meta: Meta, program: Program | None, scfg: dict) -> Quotes | None:
    if program is None or var_per_s is None or not book.yes or not book.no:
        return None
    bid, ask = book.best_yes_bid, book.yes_ask
    if bid >= ask:
        return None                                       # crossed recording
    stop = blackout_at(meta, scfg)
    if now >= stop:
        return None
    fair = microprice(book)
    if fair is None:
        return None
    tau = min(scfg["horizon_s"], stop - now)
    gamma, lot = scfg["gamma"], scfg["quote_size"]
    x_r = logit(fair) - (position / lot) * gamma * var_per_s * tau
    half = 0.5 * gamma * var_per_s * tau
    ranges = meta.price_ranges

    ref_yes = side_score(book.yes, program.target, program.discount, ranges).ref_price
    yes_bid = floor_tick(expit(x_r - half), ranges)
    if ref_yes is not None:
        yes_bid = min(yes_bid, ref_yes)
    yes_bid = min(yes_bid, round(ask - tick_size(ask, ranges), 6))

    ref_no = side_score(book.no, program.target, program.discount, ranges).ref_price
    no_target = floor_tick(1.0 - expit(x_r + half), ranges)
    no_bid = no_target if ref_no is None else min(no_target, ref_no)
    no_cross = 1.0 - bid
    no_bid = min(no_bid, round(no_cross - tick_size(no_cross, ranges), 6))

    mid = book.mid()
    cap = scfg["max_loss_per_market_frac"] * scfg["stake_usd"]
    yes_size = no_size = float(lot)
    if position >= 0:
        yes_size = min(lot, max(0.0, cap / mid - position))
    else:
        no_size = min(lot, max(0.0, cap / (1 - mid) + position))
    return Quotes(yes_bid=yes_bid if yes_size > 0 and yes_bid > 0 else None, yes_size=yes_size,
                  no_bid=no_bid if no_size > 0 and no_bid > 0 else None, no_size=no_size)

"""Turn target quotes into order actions, in Kalshi's YES-leg terms.

We think in YES bids and NO bids; Kalshi's V2 orders are bids and asks on
the YES leg. A NO bid at q is a YES ask at 1 - q.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .quoting import Quotes
from .rewards import tick_size


@dataclass
class LiveOrder:
    order_id: str
    side: str            # "yes" / "no", our terms
    price: float         # our terms: YES bid price, or NO bid price
    remaining: float
    filled: float


@dataclass
class Action:
    kind: str            # "place" | "amend" | "cancel"
    side: str
    price: float | None = None
    size: float = 0.0
    order_id: str | None = None
    amend_count: float | None = None


def to_book(side: str, price: float) -> tuple[str, float]:
    return ("bid", price) if side == "yes" else ("ask", round(1.0 - price, 4))


def from_book(book_side: str, yes_price: float) -> tuple[str, float]:
    return ("yes", yes_price) if book_side == "bid" else ("no", round(1.0 - yes_price, 4))


COUNT_GRAIN = 0.01        # Kalshi counts are fixed-point with 2 decimals


def grain(size: float) -> float:
    return math.floor(size / COUNT_GRAIN + 1e-9) * COUNT_GRAIN


def plan_actions(q: Quotes | None, live: dict[str, LiveOrder],
                 price_ranges: list[dict]) -> list[Action]:
    targets = {"yes": (q.yes_bid, q.yes_size) if q else (None, 0.0),
               "no": (q.no_bid, q.no_size) if q else (None, 0.0)}
    out = []
    for side in ("yes", "no"):
        price, size = targets[side]
        size = round(grain(size), 2)            # what Kalshi will store
        if size <= 0:
            price = None
        cur = live.get(side)
        if price is None:
            if cur:
                out.append(Action("cancel", side, order_id=cur.order_id))
            continue
        if cur is None:
            out.append(Action("place", side, price, size))
            continue
        moved = abs(cur.price - price) >= tick_size(price, price_ranges) - 1e-9
        resized = abs(cur.remaining - size) > COUNT_GRAIN / 2
        if moved or resized:
            # amend count is the total fillable: what already filled plus what should rest
            out.append(Action("amend", side, price, size, cur.order_id,
                              amend_count=cur.filled + size))
    return out

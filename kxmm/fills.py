"""Strict-queue fill model (STRATEGY.md "Fill model").

A resting YES bid is hit by takers buying NO, at the trade's YES price; a
resting NO bid is hit by takers buying YES, at 1 - YES price. A trade
through our price fills us (price priority); a trade at our price fills us
only once the displayed queue that was ahead of us is used up.
"""
from __future__ import annotations

from dataclasses import dataclass

from .book import Book
from .replay import Trade

EPS = 1e-9


@dataclass
class Resting:
    side: str            # "yes" = YES bid, "no" = NO bid
    price: float
    size: float
    queue_ahead: float


def displayed(book: Book, side: str, price: float) -> float:
    levels = book.yes if side == "yes" else book.no
    return sum(s for p, s in levels if abs(p - price) < EPS)


def place(side: str, price: float, size: float, book: Book) -> Resting:
    return Resting(side, price, size, displayed(book, side, price))


def on_book(r: Resting, book: Book) -> None:
    r.queue_ahead = min(r.queue_ahead, displayed(book, r.side, r.price))


def on_trade(r: Resting, t: Trade) -> float:
    if r.side == "yes" and t.taker_side == "no":
        level = t.yes_price
    elif r.side == "no" and t.taker_side == "yes":
        level = 1.0 - t.yes_price
    else:
        return 0.0
    if level > r.price + EPS:                     # a better level traded; we weren't reached
        return 0.0
    if level < r.price - EPS:                     # traded through us: price priority fills us
        r.queue_ahead = 0.0
        filled = min(r.size, t.count)
    else:                                         # at our price: the queue ahead goes first
        used = min(r.queue_ahead, t.count)
        r.queue_ahead -= used
        filled = min(r.size, t.count - used)
    r.size -= filled
    return filled

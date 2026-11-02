"""Fair value (microprice, in log-odds) and an EWMA of its variance per second."""
from __future__ import annotations

import math

from .book import Book

P_CLAMP = 0.001


def microprice(book: Book) -> float | None:
    """Size-weighted mid: leans toward the side with less size behind it."""
    if not book.yes or not book.no:
        return None
    bid, bid_size = book.yes[0]
    ask, ask_size = 1.0 - book.no[0][0], book.no[0][1]
    if bid_size + ask_size <= 0:
        return None
    return (bid * ask_size + ask * bid_size) / (bid_size + ask_size)


def logit(p: float) -> float:
    p = min(max(p, P_CLAMP), 1 - P_CLAMP)
    return math.log(p / (1 - p))


def expit(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


class EwmaVol:
    """EWMA of (dx)^2 / dt, weighted by elapsed time against a half-life."""

    def __init__(self, halflife_s: float):
        self.halflife_s = halflife_s
        self._last: tuple[float, float] | None = None
        self._var: float | None = None

    def update(self, ts: float, x: float) -> None:
        if self._last is not None:
            dt = ts - self._last[0]
            if dt <= 0:
                return
            sample = (x - self._last[1]) ** 2 / dt
            w = 1 - 0.5 ** (dt / self.halflife_s)
            self._var = sample if self._var is None else self._var + w * (sample - self._var)
        self._last = (ts, x)

    @property
    def var_per_s(self) -> float | None:
        return self._var

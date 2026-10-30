"""Kalshi order book: two bid ladders, YES and NO.

The API returns `orderbook_fp.yes_dollars` / `no_dollars` as [price, size]
strings, lowest price first. Here each side is best first (price descending).
A YES ask is the complement of the best NO bid: yes_ask = 1 - best_no_bid.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field


def _side(levels) -> list[tuple[float, float]]:
    out = [(float(p), float(s)) for p, s in (levels or []) if float(s) > 0]
    return sorted(out, key=lambda l: l[0], reverse=True)


@dataclass
class Book:
    yes: list[tuple[float, float]] = field(default_factory=list)
    no: list[tuple[float, float]] = field(default_factory=list)

    @classmethod
    def from_orderbook(cls, raw: dict) -> "Book":
        raw = raw or {}
        return cls(yes=_side(raw.get("yes_dollars")), no=_side(raw.get("no_dollars")))

    @property
    def best_yes_bid(self) -> float | None:
        return self.yes[0][0] if self.yes else None

    @property
    def best_no_bid(self) -> float | None:
        return self.no[0][0] if self.no else None

    @property
    def yes_ask(self) -> float | None:
        return None if self.best_no_bid is None else 1.0 - self.best_no_bid

    def mid(self) -> float | None:
        if self.best_yes_bid is None or self.yes_ask is None:
            return None
        return (self.best_yes_bid + self.yes_ask) / 2.0

    def spread(self) -> float | None:
        if self.best_yes_bid is None or self.yes_ask is None:
            return None
        return self.yes_ask - self.best_yes_bid


def book_digest(raw: dict) -> str:
    return hashlib.sha1(json.dumps(raw, sort_keys=True).encode()).hexdigest()

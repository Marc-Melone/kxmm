"""Replay one market through strategy v1.0.0 (STRATEGY.md) and the strict fill model.

Positions are signed YES contracts (Kalshi nets YES and NO): a YES-bid fill
at p buys YES; a NO-bid fill at q is a YES sale at 1 - q. Rewards accrue
between events while our orders rest, a program is live, the book is
observed and both sides (ours included) meet the target size.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .book import Book
from .fair import EwmaVol, logit, microprice
from .fills import Resting, on_book, on_trade, place
from .quoting import make_quotes
from .replay import MidSeries, events, market_meta, program_at, programs_for
from .rewards import our_side_share, side_score, tick_size

MAKER_FEE_RATE = 0.0175


@dataclass
class Fill:
    ticker: str
    ts: float
    side: str
    price: float
    size: float
    fee: float
    markouts: dict[int, float | None] = field(default_factory=dict)
    position_after: float = 0.0


@dataclass
class MarketResult:
    ticker: str
    fills: list[Fill]
    reward_by_day: dict[str, float]
    quoted_s_by_day: dict[str, float]
    position: float
    cash: float
    last_mid: float | None


def maker_fee(contracts: float, price: float) -> float:
    return math.ceil(round(MAKER_FEE_RATE * contracts * price * (1 - price) * 100, 9)) / 100


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _split_days(a: float, b: float):
    """Yield (day, seconds) pieces of [a, b) split at UTC midnights."""
    while a < b:
        midnight = (math.floor(a / 86400) + 1) * 86400
        end = min(b, midnight)
        yield _day(a), end - a
        a = end


def _with_order(levels, order: Resting | None):
    if order is None:
        return levels
    return sorted(levels + [(order.price, order.size)], reverse=True)


def simulate_market(con, ticker: str, t0: float, t1: float, cfg: dict) -> MarketResult:
    scfg = cfg["sim"]
    meta = market_meta(con, ticker)
    programs = programs_for(con, ticker)
    vol = EwmaVol(scfg["vol_halflife_s"])
    book: Book | None = None          # the book we act on (None inside gaps)
    last_book: Book | None = None     # the latest stored book, gap or not
    in_gap = False
    orders: dict[str, Resting] = {}
    position = cash = 0.0
    fills: list[Fill] = []
    reward: dict[str, float] = {}
    quoted: dict[str, float] = {}
    last_ts: float | None = None

    def accrue(now: float) -> None:
        if last_ts is None or in_gap or book is None or not orders or now <= last_ts:
            return
        prog = program_at(programs, last_ts)
        if prog is None:
            return
        y, n = orders.get("yes"), orders.get("no")
        meets = all(side_score(_with_order(lv, o), prog.target, prog.discount,
                               meta.price_ranges).meets_target
                    for lv, o in ((book.yes, y), (book.no, n)))
        share = 0.0
        if meets:
            share = sum(our_side_share(lv, o.price if o else None, o.size if o else 0.0,
                                       prog.target, prog.discount, meta.price_ranges)
                        for lv, o in ((book.yes, y), (book.no, n))) / 2
        for day, secs in _split_days(last_ts, now):
            quoted[day] = quoted.get(day, 0.0) + secs
            reward[day] = reward.get(day, 0.0) + prog.daily_pool / 86400 * secs * share

    def requote(now: float) -> None:
        if in_gap or book is None:
            return
        # the recorder stores changes only: an unchanged book means no measured
        # movement yet, so variance is 0 until the first change (STRATEGY.md)
        var = vol.var_per_s if vol.var_per_s is not None else 0.0
        q = make_quotes(book, position, var, now, meta, program_at(programs, now), scfg)
        targets = ((("yes", q.yes_bid, q.yes_size), ("no", q.no_bid, q.no_size)) if q
                   else (("yes", None, 0.0), ("no", None, 0.0)))
        for side, price, size in targets:
            if price is None:
                orders.pop(side, None)
                continue
            cur = orders.get(side)
            same_price = cur and abs(cur.price - price) < tick_size(price, meta.price_ranges) - 1e-9
            if same_price and abs(cur.size - size) < 1e-9:
                continue                              # keep queue position
            orders[side] = place(side, price, size, book)

    for e in events(con, ticker, t0, t1):
        accrue(e.ts)
        last_ts = e.ts
        if e.kind == "book":
            last_book = e.book
        if e.kind == "gap_start":
            in_gap, book = True, None
            orders.clear()
        elif e.kind == "gap_end":
            # the poll that ended the gap stored nothing if the book was unchanged,
            # so the last stored book is the book at this moment
            in_gap, book = False, last_book
            requote(e.ts)
        elif e.kind == "book" and not in_gap:
            book = e.book
            mp = microprice(book)
            if mp is not None:
                vol.update(e.ts, logit(mp))
            for o in orders.values():
                on_book(o, book)
            requote(e.ts)
        elif e.kind == "trade" and not in_gap and book is not None:
            for side, o in list(orders.items()):
                c = on_trade(o, e.trade)
                if c <= 0:
                    continue
                fee = maker_fee(c, o.price) if meta.maker_fee else 0.0
                if side == "yes":
                    position += c
                    cash -= o.price * c
                else:
                    position -= c
                    cash += (1 - o.price) * c
                cash -= fee
                fills.append(Fill(ticker, e.ts, side, o.price, c, fee, position_after=position))
                if o.size <= 1e-9:
                    orders.pop(side)
            requote(e.ts)

    # quoting continues past the last event until the last confirmed poll
    r = con.execute("SELECT last_ok FROM poll_state WHERE ticker=?", (ticker,)).fetchone()
    if r is not None and last_ts is not None:
        accrue(min(t1, r[0]))

    mids = MidSeries(con, ticker)
    for f in fills:
        for h in scfg["markouts_s"]:
            m = mids.at(f.ts + h)
            f.markouts[h] = None if m is None else (
                (m - f.price) * f.size if f.side == "yes" else ((1 - f.price) - m) * f.size)
    return MarketResult(ticker, fills, reward, quoted, position, cash, mids.at(t1))

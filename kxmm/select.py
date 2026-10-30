"""Which reward markets to record (pure functions; DESIGN.md selector rules).

prefilter -> shortlist (by pool) -> score (needs each market's book) -> choose.
The universe is sticky: a market already recorded stays while it still passes.
"""
from __future__ import annotations

from dataclasses import dataclass

from .book import Book
from .db import iso_ts, series_of
from .rewards import daily_pool_usd, est_share, side_score

MAKER_FEE_TYPE = "quadratic_with_maker_fees"


@dataclass
class Candidate:
    ticker: str
    event_ticker: str
    series_ticker: str
    daily_pool_usd: float
    target_size: float
    discount: float
    mid: float
    close_ts: float
    maker_fee: bool
    price_ranges: list[dict]


@dataclass
class Scored:
    cand: Candidate
    share_yes: float
    share_no: float
    est_daily_usd: float
    collateral_usd: float
    yield_: float


def _quote_mid(m: dict) -> float | None:
    try:
        bid, ask = float(m.get("yes_bid_dollars") or 0), float(m.get("yes_ask_dollars") or 0)
    except (TypeError, ValueError):
        return None
    if not (0 < bid <= ask < 1):
        return None
    return (bid + ask) / 2.0


def prefilter(programs: list[dict], markets: dict[str, dict], series: dict[str, dict],
              now: float, cfg: dict) -> list[Candidate]:
    sel = cfg["select"]
    best: dict[str, dict] = {}
    for p in programs:
        if p.get("incentive_type", "liquidity") != "liquidity":
            continue
        start, end = iso_ts(p.get("start_date")), iso_ts(p.get("end_date"))
        if start is None or end is None or start > now:
            continue
        if end - now < sel["min_program_hours_left"] * 3600:
            continue
        t = p["market_ticker"]
        if t not in best or daily_pool_usd(p) > daily_pool_usd(best[t]):
            best[t] = p
    out = []
    for t, p in best.items():
        m = markets.get(t)
        if not m or m.get("status") != "active" or m.get("market_type") != "binary":
            continue
        if m.get("mve_collection_ticker"):
            continue
        mid = _quote_mid(m)
        if mid is None or not (sel["mid_lo"] <= mid <= sel["mid_hi"]):
            continue
        close = iso_ts(m.get("close_time"))
        if close is None or close - now < sel["min_days_to_close"] * 86400:
            continue
        st = series_of(m.get("event_ticker", ""))
        out.append(Candidate(
            ticker=t, event_ticker=m.get("event_ticker", ""), series_ticker=st,
            daily_pool_usd=daily_pool_usd(p),
            target_size=float(p.get("target_size_fp") or 0),
            discount=(p.get("discount_factor_bps") or 0) / 10_000, mid=mid, close_ts=close,
            maker_fee=(series.get(st) or {}).get("fee_type") == MAKER_FEE_TYPE,
            price_ranges=m.get("price_ranges") or []))
    return out


def shortlist(cands: list[Candidate], n: int) -> list[Candidate]:
    return sorted(cands, key=lambda c: c.daily_pool_usd, reverse=True)[:n]


def score(cand: Candidate, book: Book, cfg: dict) -> Scored | None:
    sel = cfg["select"]
    q = sel["quote_size"]
    ys = side_score(book.yes, cand.target_size, cand.discount, cand.price_ranges)
    ns = side_score(book.no, cand.target_size, cand.discount, cand.price_ranges)
    if not (ys.meets_target and ns.meets_target):
        return None
    sy, sn = est_share(ys, q), est_share(ns, q)
    est = cand.daily_pool_usd * (sy + sn) / 2.0
    if cand.maker_fee:
        fee = sel["maker_fee_rate"] * cand.mid * (1 - cand.mid)
        if est / (2 * q) < fee:
            return None
    collateral = q * (ys.ref_price + ns.ref_price)
    return Scored(cand=cand, share_yes=sy, share_no=sn, est_daily_usd=est,
                  collateral_usd=collateral, yield_=est / collateral if collateral else 0.0)


def choose(scored: list[Scored], current: list[str], max_markets: int,
           max_per_event: int) -> list[str]:
    by_ticker = {s.cand.ticker: s for s in scored}
    chosen: list[str] = []
    per_event: dict[str, int] = {}

    def take(s: Scored) -> None:
        chosen.append(s.cand.ticker)
        per_event[s.cand.event_ticker] = per_event.get(s.cand.event_ticker, 0) + 1

    for t in current:
        if t in by_ticker and t not in chosen and len(chosen) < max_markets:
            take(by_ticker[t])
    for s in sorted(scored, key=lambda s: s.yield_, reverse=True):
        if len(chosen) >= max_markets:
            break
        if s.cand.ticker in chosen or per_event.get(s.cand.event_ticker, 0) >= max_per_event:
            continue
        take(s)
    return chosen

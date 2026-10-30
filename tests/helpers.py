"""Shared builders for synthetic Kalshi programs, markets and books."""
from datetime import datetime, timezone

NOW = 1_800_000_000
CENT = [{"start": "0.0000", "end": "1.0000", "step": "0.0100"}]
YES = [(0.50, 100), (0.49, 150), (0.48, 300), (0.47, 1000)]
NO = [(0.49, 100), (0.48, 150), (0.47, 300), (0.46, 1000)]   # YES ask 0.51


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mk(ticker, event, mid, close_days, program_hours_left, pool_usd,
       fee_type="quadratic", mve=None, market_type="binary"):
    """(program, market, series) for one market, relative to NOW."""
    start, end = NOW - 86400, NOW + program_hours_left * 3600
    days = (end - start) / 86400
    program = {"id": f"p-{ticker}-{pool_usd}", "market_ticker": ticker,
               "start_date": iso(start), "end_date": iso(end),
               "period_reward": round(pool_usd * days * 10_000),
               "target_size_fp": "1000.00", "discount_factor_bps": 5000,
               "incentive_type": "liquidity"}
    market = {"ticker": ticker, "event_ticker": event, "status": "active",
              "market_type": market_type, "mve_collection_ticker": mve,
              "yes_bid_dollars": f"{mid - 0.01:.4f}", "yes_ask_dollars": f"{mid + 0.01:.4f}",
              "close_time": iso(NOW + close_days * 86400), "price_ranges": CENT}
    series = {"ticker": event.split("-")[0], "fee_type": fee_type, "category": "X"}
    return program, market, series


def raw_book(yes, no):
    """API-shaped orderbook_fp (lowest price first, strings) from best-first levels."""
    def side(levels):
        return [[f"{p:.4f}", f"{s:.2f}"] for p, s in sorted(levels)]
    return {"yes_dollars": side(yes), "no_dollars": side(no)}

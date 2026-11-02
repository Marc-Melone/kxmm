"""In-memory stand-in for KalshiTrade (demo exchange) used by trader and drill tests."""
import itertools
from datetime import datetime, timezone

from kxmm.kalshi_public import Unavailable
from tests.helpers import CENT, iso


class FakeTradeClient:
    is_demo = True

    def __init__(self, books=None, markets=None, now=lambda: 1_800_000_000.0):
        self.books = dict(books or {})          # ticker -> orderbook_fp raw
        self.market_rows = list(markets or [])
        self.orders: dict[str, dict] = {}
        self.pos: dict[str, float] = {}
        self.fill_rows: list[dict] = []
        self.misreport: dict[str, float] = {}
        self.status = {"trading_active": True, "exchange_active": True}
        self.fail_create: set[str] = set()      # tickers whose create_order raises
        self.fail_book: set[str] = set()
        self.calls: list[tuple] = []
        self.groups: dict[str, dict] = {}
        self.now = now
        self._ids = itertools.count(1)
        self.on_attempt = None

    # --- orders ---
    def create_order(self, ticker, book_side, price, count, client_order_id, order_group_id):
        self.calls.append(("create", ticker, book_side, round(price, 4), count))
        if ticker in self.fail_create:
            from kxmm.kalshi_trade import TradeError
            raise TradeError("HTTP 409 on POST /portfolio/events/orders: post_only_cross")
        oid = f"o{next(self._ids)}"
        self.orders[oid] = {"order_id": oid, "ticker": ticker, "book_side": book_side,
                            "price": round(price, 4), "remaining": float(count), "filled": 0.0,
                            "group": order_group_id}
        return {"order_id": oid}

    def amend_order(self, order_id, ticker, book_side, price, count):
        self.calls.append(("amend", ticker, book_side, round(price, 4), count))
        o = self.orders[order_id]
        o["price"], o["remaining"] = round(price, 4), float(count) - o["filled"]
        return {"order_id": order_id}

    def batch_cancel(self, order_ids):
        self.calls.append(("cancel", tuple(order_ids)))
        for oid in order_ids:
            self.orders.pop(oid, None)
        return {"orders": []}

    def cancel_all(self):
        self.calls.append(("cancel_all",))
        self.orders.clear()

    def resting_orders(self, ticker=None):
        return [{"order_id": o["order_id"], "ticker": o["ticker"], "book_side": o["book_side"],
                 "yes_price_dollars": f"{o['price']:.4f}",
                 "remaining_count_fp": f"{o['remaining']:.2f}",
                 "fill_count_fp": f"{o['filled']:.2f}", "status": "resting",
                 "order_group_id": o["group"]}
                for o in self.orders.values() if ticker in (None, o["ticker"])]

    def take(self, ticker, book_side, price, count):
        self.calls.append(("take", ticker, book_side, round(price, 4), count))
        self._record_fill(ticker, book_side, price, count, order_id="taker")
        return {"order_id": "taker", "fill_count": f"{count:.2f}"}

    # --- helpers for tests ---
    def fill(self, order_id, count):
        o = self.orders[order_id]
        o["filled"] += count
        o["remaining"] -= count
        self._record_fill(o["ticker"], o["book_side"], o["price"], count, order_id)
        if o["remaining"] <= 1e-9:
            self.orders.pop(order_id)

    def _record_fill(self, ticker, book_side, price, count, order_id):
        sign = 1 if book_side == "bid" else -1
        self.pos[ticker] = self.pos.get(ticker, 0.0) + sign * count
        self.fill_rows.append({"fill_id": f"f{next(self._ids)}", "ticker": ticker,
                               "book_side": book_side, "yes_price_dollars": f"{price:.4f}",
                               "count_fp": f"{count:.2f}", "fee_cost": "0.0000",
                               "order_id": order_id, "created_time": iso(self.now())})

    # --- portfolio ---
    def fills(self, min_ts):
        return [f for f in self.fill_rows
                if datetime.fromisoformat(f["created_time"].replace("Z", "+00:00"))
                .timestamp() >= min_ts]

    def positions(self):
        pos = dict(self.pos, **self.misreport)
        return [{"ticker": t, "position_fp": f"{p:.2f}"} for t, p in pos.items()]

    # --- groups / status / data ---
    def create_order_group(self, contracts_limit):
        gid = f"g{next(self._ids)}"
        self.groups[gid] = {"limit": contracts_limit, "triggered": False}
        return gid

    def reset_order_group(self, gid):
        self.groups[gid]["triggered"] = False

    def trigger_order_group(self, gid):
        self.groups[gid]["triggered"] = True
        for oid in [o for o, v in self.orders.items() if v["group"] == gid]:
            self.orders.pop(oid)

    def exchange_status(self):
        return dict(self.status)

    def orderbook(self, ticker):
        if ticker in self.fail_book:
            raise Unavailable("network down")
        return self.books.get(ticker, {"yes_dollars": [], "no_dollars": []})

    def markets(self, **filters):
        tickers = filters.get("tickers")
        rows = self.market_rows
        if tickers:
            rows = [m for m in rows if m["ticker"] in tickers.split(",")]
        return rows


def market(ticker, event="EK-1", close_days=10, now=1_800_000_000.0):
    return {"ticker": ticker, "event_ticker": event, "status": "active", "market_type": "binary",
            "mve_collection_ticker": None, "close_time": iso(now + close_days * 86400),
            "price_ranges": CENT, "yes_bid_dollars": "0.0000", "yes_ask_dollars": "0.0000"}


def utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc)

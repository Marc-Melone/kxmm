"""Signed Kalshi trading client — DEMO ONLY in Phase 3.

Production base URLs are refused at construction; Phase 4 adds them behind
explicit arming. Credentials never appear in any message this module raises.
Drill fault files (FAULT_NET, FAULT_429) are honoured only on demo.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import requests

from .kalshi_public import NotFound, RateLimiter, Unavailable

log = logging.getLogger("kxmm.trade")

DEMO_BASES = (
    "https://external-api.demo.kalshi.co/trade-api/v2",
    "https://demo-api.kalshi.co/trade-api/v2",
)
PREFIX = "/trade-api/v2"


class TradeError(RuntimeError):
    """A request Kalshi refused. Built from status and body only, never headers."""


def _px(p: float) -> str:
    return f"{p:.4f}"


def _ct(c: float) -> str:
    return f"{c:.2f}"


class KalshiTrade:
    def __init__(self, base_url: str, signer, rate_per_sec: float = 8.0, timeout_s: float = 20,
                 max_attempts: int = 4, session=None, sleep=time.sleep,
                 fault_dir: Path | None = None):
        self._base = base_url.rstrip("/")
        if self._base not in DEMO_BASES:
            raise ValueError("Phase 3 trades on Kalshi's demo exchange only; "
                             f"refusing base URL {self._base}")
        self._signer = signer
        self._session = session or requests.Session()
        self._timeout, self._max_attempts, self._sleep = timeout_s, max_attempts, sleep
        self._rl = RateLimiter(rate_per_sec, sleep)
        self._fault_dir = Path(fault_dir) if fault_dir else None
        self.on_attempt = None

    @property
    def is_demo(self) -> bool:
        return self._base in DEMO_BASES

    def _fault(self) -> str | None:
        if not (self._fault_dir and self.is_demo):
            return None
        if (self._fault_dir / "FAULT_NET").exists():
            return "net"
        if (self._fault_dir / "FAULT_429").exists():
            return "429"
        return None

    def _request(self, method: str, path: str, params: dict | None = None,
                 body: dict | None = None, idempotent: bool = True) -> dict:
        """Retries 429 always. A new order (idempotent=False) is never re-sent after a
        network error or 5xx: it may already have reached Kalshi."""
        last = "no attempt"
        for attempt in range(self._max_attempts):
            self._rl.acquire()
            r, fault = None, self._fault()
            if fault == "net":
                last = "network fault (drill)"
            elif fault == "429":
                last = "HTTP 429 (drill)"
            else:
                try:
                    r = self._session.request(method, self._base + path, params=params, json=body,
                                              headers=self._signer.headers(method, PREFIX + path),
                                              timeout=self._timeout)
                except requests.RequestException as e:
                    last = type(e).__name__
                    if not idempotent:
                        raise Unavailable(f"{method} {path}: {last}; it may have reached "
                                          "Kalshi, so it was not re-sent") from None
            if self.on_attempt:
                self.on_attempt()
            if r is not None:
                code = r.status_code
                if code in (200, 201, 204):
                    try:
                        return r.json() or {}
                    except ValueError:
                        return {}
                if code == 401:
                    raise TradeError("401 unauthorized: check the API key and the system clock")
                if code == 404:
                    raise NotFound(path)
                if code != 429 and code < 500:
                    try:
                        b = r.json() or {}
                    except ValueError:
                        b = {}
                    raise TradeError(f"HTTP {code} on {method} {path}: "
                                     f"{b.get('code', '')} {b.get('message', '')}".strip())
                last = f"HTTP {code}"
                if code >= 500 and not idempotent:
                    raise Unavailable(f"{method} {path}: {last}; it may have reached Kalshi, "
                                      "so it was not re-sent")
            if attempt < self._max_attempts - 1:
                self._sleep(2 ** attempt)
        raise Unavailable(f"{method} {path} failed after {self._max_attempts} attempts ({last})")

    def _paged(self, path: str, key: str, params: dict) -> list[dict]:
        out, cursor = [], ""
        for _ in range(100):
            p = dict(params, limit=1000)
            if cursor:
                p["cursor"] = cursor
            d = self._request("GET", path, params=p)
            batch = d.get(key) or []
            out.extend(batch)
            cursor = d.get("cursor") or ""
            if not cursor or not batch:
                return out
        log.warning("%s: stopped after 100 pages", path)
        return out

    # --- orders ------------------------------------------------------------
    def create_order(self, ticker: str, book_side: str, price: float, count: float,
                     client_order_id: str, order_group_id: str | None,
                     expiration_time: int | None = None) -> dict:
        body = {"ticker": ticker, "side": book_side, "price": _px(price), "count": _ct(count),
                "time_in_force": "good_till_canceled", "post_only": True,
                "cancel_order_on_pause": True, "self_trade_prevention_type": "taker_at_cross",
                "client_order_id": client_order_id}
        if order_group_id:
            body["order_group_id"] = order_group_id
        if expiration_time is not None:
            body["expiration_time"] = int(expiration_time)
        return self._request("POST", "/portfolio/events/orders", body=body, idempotent=False)

    def take(self, ticker: str, book_side: str, price: float, count: float) -> dict:
        """Immediate-or-cancel taker order. Drills only: the strategy never takes."""
        return self._request("POST", "/portfolio/events/orders", body={
            "ticker": ticker, "side": book_side, "price": _px(price), "count": _ct(count),
            "time_in_force": "immediate_or_cancel", "self_trade_prevention_type": "taker_at_cross"},
            idempotent=False)

    def amend_order(self, order_id: str, ticker: str, book_side: str, price: float,
                    count: float, expiration_time: int | None = None) -> dict:
        body = {"ticker": ticker, "side": book_side, "price": _px(price), "count": _ct(count)}
        if expiration_time is not None:
            body["expiration_time"] = int(expiration_time)
        return self._request("POST", f"/portfolio/events/orders/{order_id}/amend", body=body)

    def batch_cancel(self, orders: list[tuple[str, str]]) -> list[str]:
        """orders: [(order_id, market_ticker)]. Returns the ids Kalshi failed to cancel
        (failures come back per order inside an HTTP 200)."""
        d = self._request("DELETE", "/portfolio/events/orders/batched", body={
            "orders": [{"order_id": o, "market_ticker": t} for o, t in orders]})
        return [r.get("order_id") for r in d.get("orders") or [] if r.get("error")]

    def cancel_all(self) -> None:
        self._request("DELETE", "/portfolio/events/orders")

    def resting_orders(self, ticker: str | None = None) -> list[dict]:
        params = {"status": "resting"}
        if ticker:
            params["ticker"] = ticker
        return self._paged("/portfolio/orders", "orders", params)

    # --- portfolio ---------------------------------------------------------
    def fills(self, min_ts: int) -> list[dict]:
        return self._paged("/portfolio/fills", "fills", {"min_ts": int(min_ts)})

    def positions(self) -> list[dict]:
        return self._paged("/portfolio/positions", "market_positions", {})

    # --- order groups ------------------------------------------------------
    def create_order_group(self, contracts_limit: int) -> str:
        return self._request("POST", "/portfolio/order_groups/create",
                             body={"contracts_limit": int(contracts_limit)})["order_group_id"]

    def trigger_order_group(self, group_id: str) -> None:
        """Cancel every order in the group now (drills; Kalshi does this itself on a burst)."""
        self._request("PUT", f"/portfolio/order_groups/{group_id}/trigger")

    def reset_order_group(self, group_id: str) -> None:
        self._request("PUT", f"/portfolio/order_groups/{group_id}/reset")

    # --- market data -------------------------------------------------------
    def exchange_status(self) -> dict:
        return self._request("GET", "/exchange/status")

    def orderbook(self, ticker: str) -> dict:
        return self._request("GET", f"/markets/{ticker}/orderbook").get("orderbook_fp") or {}

    def markets(self, **filters) -> list[dict]:
        return self._paged("/markets", "markets", {k: v for k, v in filters.items()
                                                   if v is not None})

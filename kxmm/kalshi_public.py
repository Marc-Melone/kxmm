"""Kalshi public market-data client (unauthenticated, read-only).

Adapted from kxsurv's KalshiPublic: a local rate cap and bounded exponential
backoff on 429/5xx and network errors. No key, no orders.
"""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger("kxmm.kalshi")

SUPPORTED_BASES = (
    "https://api.elections.kalshi.com/trade-api/v2",
    "https://external-api.kalshi.com/trade-api/v2",
)


TRADES_PAGE = 1000   # trades per page requested from /markets/trades


class NotFound(Exception):
    """The market (or other resource) no longer exists: HTTP 404."""


class Unavailable(RuntimeError):
    """Kalshi kept failing after every retry."""


class RateLimiter:
    def __init__(self, rate_per_sec: float, sleep=time.sleep):
        self._min_interval = 1.0 / rate_per_sec
        self._last = None
        self._sleep = sleep

    def acquire(self) -> None:
        now = time.monotonic()
        if self._last is not None:
            wait = self._min_interval - (now - self._last)
            if wait > 0:
                self._sleep(wait)
                now += wait
        self._last = now


class KalshiPublic:
    def __init__(self, base_url: str, rate_per_sec: float, timeout_s: float,
                 max_attempts: int, session=None, sleep=time.sleep):
        self._base = base_url.rstrip("/")
        if self._base not in SUPPORTED_BASES:
            raise ValueError(f"unsupported Kalshi base URL: {base_url}")
        self._session = session or requests.Session()
        self._timeout = timeout_s
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._rl = RateLimiter(rate_per_sec, sleep)
        self.on_attempt = None   # called after every HTTP attempt, failed ones too

    def _get(self, path: str, params: dict | None = None) -> dict:
        last = "no attempt"
        for attempt in range(self._max_attempts):
            self._rl.acquire()
            try:
                r = self._session.get(self._base + path, params=params,
                                      timeout=self._timeout)
            except requests.RequestException as e:
                r, last = None, f"{type(e).__name__}: {e}"
            if self.on_attempt:
                self.on_attempt()
            if r is not None:
                if r.status_code == 404:
                    raise NotFound(path)
                if r.status_code != 429 and r.status_code < 500:
                    r.raise_for_status()
                    return r.json()
                last = f"HTTP {r.status_code}"
            if attempt < self._max_attempts - 1:
                self._sleep(2 ** attempt)
        raise Unavailable(f"{path} failed after {self._max_attempts} attempts ({last})")

    def incentive_programs(self, status: str = "active", type_: str = "liquidity") -> list[dict]:
        out: list[dict] = []
        cursor = ""
        while True:
            params = {"status": status, "type": type_, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            d = self._get("/incentive_programs", params)
            batch = d.get("incentive_programs") or []
            out.extend(batch)
            cursor = d.get("next_cursor") or ""
            if not cursor or not batch:
                return out

    def markets_by_tickers(self, tickers: list[str], batch: int) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(tickers), batch):
            chunk = tickers[i:i + batch]
            d = self._get("/markets", {"tickers": ",".join(chunk), "limit": len(chunk)})
            out.extend(d.get("markets") or [])
        return out

    def series(self, ticker: str) -> dict:
        return self._get("/series/" + ticker).get("series") or {}

    def market(self, ticker: str) -> dict:
        return self._get("/markets/" + ticker).get("market") or {}

    def orderbook(self, ticker: str) -> dict:
        return self._get(f"/markets/{ticker}/orderbook").get("orderbook_fp") or {}

    def trades(self, ticker: str, min_ts: int, max_pages: int) -> list[dict]:
        out: list[dict] = []
        cursor = ""
        for _ in range(max_pages):
            params = {"ticker": ticker, "min_ts": int(min_ts), "limit": TRADES_PAGE}
            if cursor:
                params["cursor"] = cursor
            d = self._get("/markets/trades", params)
            batch = d.get("trades") or []
            out.extend(batch)
            cursor = d.get("cursor") or ""
            if not cursor or not batch:
                return out
        log.warning("trades %s: stopped at %d pages; older trades in this window skipped",
                    ticker, max_pages)
        return out

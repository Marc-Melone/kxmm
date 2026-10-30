"""Phase 1 recorder: pick reward markets hourly, record books and trades.

Single-threaded. One failing market never stops the cycle; a market whose
book 404s leaves the universe. `out/KILL` stops the loop (Windows can't
deliver SIGTERM to a console process). The heartbeat is refreshed between
requests, so a long universe refresh never looks like a dead loop.
"""
from __future__ import annotations

import logging
from pathlib import Path

from . import db
from .book import Book
from .kalshi_public import NotFound
from .keepawake import keep_awake
from .select import choose, prefilter, score, shortlist

log = logging.getLogger("kxmm.recorder")

SERIES_TTL_S = 86400
REFRESH_RETRY_S = 300
SUMMARY_EVERY_S = 600


class Recorder:
    def __init__(self, cfg: dict, client, con, root: Path, clock=None, sleep=None,
                 awake=keep_awake):
        import time
        self.cfg, self.client, self.con = cfg, client, con
        self.rec = cfg["record"]
        self.kill_path = Path(root) / "out" / "KILL"
        self.clock = clock or time.time
        self.sleep = sleep or time.sleep
        self.awake = awake
        self.universe: list[str] = db.last_selection(con)
        self._series: dict[str, tuple[float, dict]] = {}

    # --- bookkeeping -------------------------------------------------------
    def _beat(self) -> None:
        db.heartbeat(self.con, self.clock(), self.rec["gap_threshold_s"])

    def _killed(self) -> bool:
        if self.kill_path.exists():
            self.kill_path.unlink(missing_ok=True)
            log.info("stopped by KILL")
            return True
        return False

    def _series_for(self, tickers: set[str], now: float) -> dict[str, dict]:
        for st in tickers:
            hit = self._series.get(st)
            if hit and now - hit[0] < SERIES_TTL_S:
                continue
            try:
                self._series[st] = (now, self.client.series(st))
            except Exception as e:  # noqa: BLE001
                log.warning("series %s: %s", st, e)
            self._beat()
        return {st: v[1] for st, v in self._series.items()}

    # --- work --------------------------------------------------------------
    def refresh_universe(self, now: float) -> list[str]:
        t0 = self.clock()
        programs = self.client.incentive_programs()
        self._beat()
        tickers = sorted({p["market_ticker"] for p in programs})
        markets = {m["ticker"]: m
                   for m in self.client.markets_by_tickers(tickers, self.cfg["api"]["tickers_batch"])}
        self._beat()
        # series only for markets that pass every other rule
        needed = {c.series_ticker for c in prefilter(programs, markets, {}, now, self.cfg)}
        series = self._series_for(needed, now)
        cands = shortlist(prefilter(programs, markets, series, now, self.cfg),
                          self.rec["shortlist_n"])
        scored = []
        for c in cands:
            try:
                s = score(c, Book.from_orderbook(self.client.orderbook(c.ticker)), self.cfg)
            except Exception as e:  # noqa: BLE001
                log.debug("score %s skipped: %s", c.ticker, e)
                s = None
            if s:
                scored.append(s)
            self._beat()
        self.universe = choose(scored, self.universe, self.rec["max_markets"],
                               self.rec["max_per_event"])
        by = {s.cand.ticker: s for s in scored}
        db.upsert_programs(self.con, programs, now)
        db.upsert_markets(self.con, [markets[t] for t in self.universe if t in markets],
                          series, now)
        db.log_selection(self.con, now, self.universe, [
            {"ticker": t, "pool_usd_day": round(by[t].cand.daily_pool_usd, 2),
             "est_usd_day": round(by[t].est_daily_usd, 4), "yield": round(by[t].yield_, 5)}
            for t in self.universe])
        log.info("universe refreshed: %d markets (%d programs, %d candidates, %d scored) in %.0fs",
                 len(self.universe), len(programs), len(cands), len(scored), self.clock() - t0)
        return self.universe

    def poll_books(self, now: float) -> None:
        for t in list(self.universe):
            try:
                raw = self.client.orderbook(t)
            except NotFound:
                log.info("book %s: 404, dropped from universe", t)
                self.universe.remove(t)
                continue
            except Exception as e:  # noqa: BLE001
                log.warning("book %s: %s", t, e)
                continue
            db.store_book(self.con, t, self.clock(), raw)
            db.mark_poll(self.con, t, self.clock(), self.rec["gap_threshold_s"])
            self._beat()

    def poll_trades(self, now: float) -> None:
        for t in list(self.universe):
            last = db.kv_get(self.con, f"trades_ts:{t}")
            min_ts = int(float(last) if last else now - self.rec["trades_backfill_s"]) - 1
            try:
                rows = self.client.trades(t, min_ts, self.rec["trades_max_pages"])
            except Exception as e:  # noqa: BLE001
                log.warning("trades %s: %s", t, e)
                continue
            if rows:
                db.store_trades(self.con, rows)
                newest = max(db.iso_ts(r["created_time"]) for r in rows)
                db.kv_set(self.con, f"trades_ts:{t}", repr(newest))
            self._beat()

    def _summary(self, since: float) -> None:
        q = lambda sql: self.con.execute(sql, (since,)).fetchone()[0]  # noqa: E731
        log.info("last %.0f min: %d markets, %d books, %d trades, %d gaps",
                 SUMMARY_EVERY_S / 60, len(self.universe),
                 q("SELECT COUNT(*) FROM books WHERE ts >= ?"),
                 q("SELECT COUNT(*) FROM trades WHERE ts >= ?"),
                 q("SELECT COUNT(*) FROM gaps WHERE end_ts >= ?"))

    def run(self) -> None:
        thr = self.rec["gap_threshold_s"]
        with self.awake():
            now = self.clock()
            n = db.record_restart_gaps(self.con, now, thr, tickers=self.universe)
            if n:
                log.info("restart: recorded %d gaps for downtime", n)
            next_refresh = next_books = next_trades = now
            next_summary = now + SUMMARY_EVERY_S
            while True:
                if self._killed():
                    return
                now = self.clock()
                if now >= next_refresh:
                    try:
                        self.refresh_universe(now)
                        next_refresh = now + self.rec["universe_refresh_s"]
                    except Exception as e:  # noqa: BLE001
                        log.warning("universe refresh failed, retry in %ds: %s",
                                    REFRESH_RETRY_S, e)
                        next_refresh = now + REFRESH_RETRY_S
                if now >= next_books:
                    self.poll_books(now)
                    next_books = now + self.rec["book_poll_s"]
                if now >= next_trades:
                    self.poll_trades(now)
                    next_trades = now + self.rec["trades_poll_s"]
                self._beat()
                if self.clock() >= next_summary:
                    self._summary(next_summary - SUMMARY_EVERY_S)
                    next_summary += SUMMARY_EVERY_S
                wake = min(next_refresh, next_books, next_trades)
                while self.clock() < wake:
                    if self._killed():
                        return
                    self.sleep(min(1.0, wake - self.clock()))

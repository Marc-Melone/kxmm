"""Demo trader loop (Phase 3). Same quoting rules as the simulator (STRATEGY.md),
real orders on Kalshi's demo exchange, and every safety stop from DESIGN.md.

Files in out/trade/: KILL stops the loop (cancel all, exit); HALT stops quoting
(cancel all, keep the heartbeat) until a person deletes it.
"""
from __future__ import annotations

import logging
import math
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import tradedb
from .book import Book
from .db import iso_ts
from .fair import EwmaVol, logit, microprice
from .keepawake import keep_awake
from .orders import LiveOrder, from_book, plan_actions, to_book
from .quoting import Quotes, make_quotes
from .replay import Meta, Program
from .rewards import tick_size
from .risk import Pnl, event_room, loss_state, reconcile, seed_positions

log = logging.getLogger("kxmm.trader")
MIN_DAYS_TO_CLOSE = 3


def _utc(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc)


class Trader:
    def __init__(self, cfg: dict, client, con, root: Path, clock=time.time, sleep=time.sleep,
                 awake=keep_awake):
        self.tc = cfg["trade"]
        self.scfg = {**cfg["sim"], "quote_size": self.tc["quote_size"],
                     "stake_usd": self.tc["stake_usd"]}
        self.client, self.con, self.clock, self.sleep, self.awake = client, con, clock, sleep, awake
        self.dir = Path(root) / "out" / "trade"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.kill, self.halt = self.dir / "KILL", self.dir / "HALT"
        self.program = Program(start=0.0, end=math.inf, daily_pool=0.0,
                               target=self.tc["demo_target"], discount=0.5)
        self.pnl = Pnl()
        self.meta: dict[str, Meta] = {}
        self.event_of: dict[str, str] = {}
        self.groups: dict[str, str] = {}
        self.vol: dict[str, EwmaVol] = {}
        self.books: dict[str, Book] = {}
        self.book_ts: dict[str, float] = {}
        self.stale: set[str] = set()
        self.known_ids: set[str] = set()
        self.fill_since = 0.0
        self.next_status = self.next_reconcile = 0.0
        self.trading_ok = True
        self.halted = False
        self.paused_until = 0.0
        self.day_key = self.week_key = None
        self.day_start = self.week_start = 0.0
        if hasattr(client, "on_attempt"):
            client.on_attempt = lambda: tradedb.heartbeat(self.con, self.clock())

    # --- helpers ---------------------------------------------------------
    def _event(self, kind: str, detail: str = "") -> None:
        tradedb.event(self.con, self.clock(), kind, detail)
        (log.warning if kind in ("error", "halt", "reconcile_bad", "stale") else log.info)(
            "%s %s", kind, detail)

    def _write_halt(self, reason: str) -> None:
        self.halt.write_text(reason + "\n", encoding="utf-8")
        self.client.cancel_all()
        self.halted = True
        self._event("halt", reason)

    def _pick_markets(self, now: float) -> list[dict]:
        if self.tc["markets"]:
            return self.client.markets(tickers=",".join(self.tc["markets"]))
        rows = [m for m in self.client.markets(status="open", mve_filter="exclude")
                if m.get("market_type") == "binary" and not m.get("mve_collection_ticker")
                and (iso_ts(m.get("close_time")) or 0) - now >= MIN_DAYS_TO_CLOSE * 86400]
        two = [m for m in rows if float(m.get("yes_bid_dollars") or 0) > 0
               and 0 < float(m.get("yes_ask_dollars") or 0) < 1]
        rest = [m for m in rows if m not in two]
        return (two + rest)[: self.tc["max_markets"]]

    def _plumbing(self, book: Book, now: float, ranges: list[dict]) -> Quotes:
        k = int(now // self.tc["churn_s"]) % 2
        bid = round(self.tc["plumbing_bid"] + k * tick_size(self.tc["plumbing_bid"], ranges), 4)
        ask = round(self.tc["plumbing_ask"] - k * tick_size(self.tc["plumbing_ask"], ranges), 4)
        if book.yes_ask is not None and bid >= book.yes_ask:
            bid = None                             # would cross a resting ask
        if book.best_yes_bid is not None and ask <= book.best_yes_bid:
            ask = None
        return Quotes(bid, 1.0 if bid else 0.0, round(1 - ask, 4) if ask else None,
                      1.0 if ask else 0.0)

    def _live(self, resting: list[dict], ticker: str) -> dict[str, LiveOrder]:
        out: dict[str, LiveOrder] = {}
        for o in resting:
            if o["ticker"] != ticker:
                continue
            side, price = from_book(o["book_side"], float(o["yes_price_dollars"]))
            lo = LiveOrder(o["order_id"], side, price, float(o["remaining_count_fp"]),
                           float(o.get("fill_count_fp") or 0))
            if side in out:                        # a duplicate on one side: cancel it
                self.client.batch_cancel([lo.order_id])
                continue
            out[side] = lo
        return out

    def _cancel_market(self, resting: list[dict], ticker: str) -> None:
        ids = [o["order_id"] for o in resting if o["ticker"] == ticker]
        if ids:
            self.client.batch_cancel(ids)

    # --- lifecycle -------------------------------------------------------
    def start(self) -> None:
        now = self.clock()
        self.client.cancel_all()                   # never inherit orders we didn't place
        self._event("start", "")
        for m in self._pick_markets(now):
            t = m["ticker"]
            self.meta[t] = Meta(t, m.get("price_ranges") or [], iso_ts(m.get("close_time")) or 0,
                                iso_ts(m.get("occurrence_datetime")), False)
            self.event_of[t] = m.get("event_ticker", t)
            self.vol[t] = EwmaVol(self.scfg["vol_halflife_s"])
            self.groups[t] = self.client.create_order_group(
                int(self.tc["group_limit_mult"] * self.tc["quote_size"]))
        seed_positions(self.pnl, {p["ticker"]: float(p["position_fp"])
                                  for p in self.client.positions()})
        self.fill_since = now
        self.next_reconcile = now + self.tc["reconcile_s"]
        log.info("trading demo markets: %s", ", ".join(self.meta) or "(none)")

    def cycle(self, now: float) -> None:
        if self.halt.exists():
            if not self.halted:
                self.client.cancel_all()
                self.halted = True
                self._event("halt", self.halt.read_text(encoding="utf-8").strip())
            tradedb.heartbeat(self.con, now)
            return
        self.halted = False
        if now >= self.next_status:
            self.trading_ok = bool(self.client.exchange_status().get("trading_active"))
            self.next_status = now + self.tc["status_poll_s"]
            if not self.trading_ok:
                self.client.cancel_all()
        for t in self.meta:                        # books, volatility, staleness
            try:
                b = Book.from_orderbook(self.client.orderbook(t))
                self.books[t], self.book_ts[t] = b, now
                self.stale.discard(t)
                mp = microprice(b)
                if mp is not None:
                    self.vol[t].update(now, logit(mp))
            except Exception as e:  # noqa: BLE001
                log.warning("book %s: %s", t, e)
        resting = self.client.resting_orders()
        self._ingest_fills()
        marks = {t: b.mid() for t, b in self.books.items() if b.mid() is not None}
        if self._loss_check(now, marks):
            resting = []
        quoting = self.trading_ok and now >= self.paused_until and not self.halted
        for t, meta in self.meta.items():
            if now - self.book_ts.get(t, -math.inf) > self.tc["stale_book_s"]:
                if t not in self.stale:
                    self.stale.add(t)
                    self._cancel_market(resting, t)
                    self._event("stale", f"{t}: no book for {self.tc['stale_book_s']} s")
                continue
            if not quoting:
                continue
            self._quote(t, meta, now, resting, marks)
        if now >= self.next_reconcile:
            self._reconcile()
            self.next_reconcile = now + self.tc["reconcile_s"]
        tradedb.heartbeat(self.con, now)

    def _quote(self, t: str, meta: Meta, now: float, resting: list[dict],
               marks: dict[str, float]) -> None:
        book, pos = self.books[t], self.pnl.position(t)
        var = self.vol[t].var_per_s if self.vol[t].var_per_s is not None else 0.0
        q = make_quotes(book, pos, var, now, meta, self.program, self.scfg)
        if q is None and not (book.yes and book.no):
            q = self._plumbing(book, now, meta.price_ranges)
        if q is not None and event_room(self.pnl.positions(), marks, self.event_of, t,
                                        self.tc) <= 0:
            if pos >= 0:                           # pull the side that would add exposure
                q = Quotes(None, 0.0, q.no_bid, q.no_size)
            else:
                q = Quotes(q.yes_bid, q.yes_size, None, 0.0)
        for a in plan_actions(q, self._live(resting, t), meta.price_ranges):
            try:
                if a.kind == "place":
                    bs, px = to_book(a.side, a.price)
                    r = self.client.create_order(t, bs, px, a.size, str(uuid.uuid4()),
                                                 self.groups.get(t))
                    self.known_ids.add(r.get("order_id"))
                elif a.kind == "amend":
                    bs, px = to_book(a.side, a.price)
                    self.client.amend_order(a.order_id, t, bs, px, a.amend_count)
                else:
                    self.client.batch_cancel([a.order_id])
                    self.known_ids.discard(a.order_id)
            except Exception as e:  # noqa: BLE001 — one failed action never stops the loop
                self._event("error", f"{t} {a.kind} {a.side}: {e}")

    def _ingest_fills(self) -> None:
        new = tradedb.add_fills(self.con, self.client.fills(min_ts=int(self.fill_since) - 60))
        for f in new:
            self.pnl.apply_fill(f["ticker"], f["book_side"], float(f["yes_price_dollars"]),
                                float(f["count_fp"]), float(f.get("fee_cost") or 0))
            ts = iso_ts(f.get("created_time"))
            if ts:
                self.fill_since = max(self.fill_since, ts)

    def _loss_check(self, now: float, marks: dict[str, float]) -> bool:
        """True when it cancelled everything this cycle."""
        eq = self.pnl.equity(marks)
        d = _utc(now)
        if d.date() != self.day_key:
            self.day_key, self.day_start = d.date(), eq
        if d.isocalendar()[:2] != self.week_key:
            self.week_key, self.week_start = d.isocalendar()[:2], eq
        state = loss_state(eq, self.day_start, self.week_start, self.tc)
        if state == "halt":
            self._write_halt(f"weekly loss stop: equity {eq - self.week_start:+.2f} this week")
            return True
        if state == "pause_day" and now >= self.paused_until:
            self.client.cancel_all()
            self.paused_until = (now // 86400 + 1) * 86400
            self._event("pause", f"daily loss stop: equity {eq - self.day_start:+.2f} today")
            return True
        return False

    def _reconcile(self) -> None:
        remote = self.client.resting_orders()
        remote_ids = {o["order_id"] for o in remote}
        self.known_ids &= remote_ids               # filled or cancelled orders are simply gone
        local = self.pnl.positions()
        first = next(iter(self.meta), None)
        if first and self.client.is_demo and (self.dir / "FAULT_PHANTOM").exists():
            local = {**local, first: local.get(first, 0.0) + 1}   # reconcile drill only
        remote_pos = {p["ticker"]: float(p["position_fp"]) for p in self.client.positions()}
        bad = reconcile(local, remote_pos, self.known_ids, remote_ids)
        if bad:
            self._event("reconcile_bad", "; ".join(bad))
            self._write_halt("reconcile: " + "; ".join(bad))
        else:
            self._event("reconcile_ok", "")

    def run(self) -> None:
        with self.awake():
            self.start()
            while True:
                if self.kill.exists():
                    self.client.cancel_all()
                    self._event("stop", "KILL")
                    tradedb.kv_set(self.con, "stopped", repr(self.clock()))
                    self.kill.unlink(missing_ok=True)
                    return
                try:
                    self.cycle(self.clock())
                except Exception as e:  # noqa: BLE001 — record, keep going
                    self._event("error", f"cycle: {type(e).__name__}: {e}")
                wake = self.clock() + self.tc["book_poll_s"]
                while self.clock() < wake and not self.kill.exists():
                    self.sleep(min(1.0, wake - self.clock()))

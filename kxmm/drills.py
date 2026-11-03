"""Demo drills: prove every safety path against Kalshi's demo exchange.

Standalone: orders, group, fill. With the trader running: kill, halt,
network, ratelimit, reconcile. Each result lands in trade.db `drills`.
"""
from __future__ import annotations

import functools
import uuid
from pathlib import Path

from . import tradedb
from .rewards import tick_size

DRILLS_NEEDING_LOOP = ("kill", "halt", "network", "ratelimit", "reconcile")
FAULT_S = 60


def _wait(cond, clock, sleep, timeout: float) -> bool:
    deadline = clock() + timeout
    while clock() < deadline:
        if cond():
            return True
        sleep(1)
    return cond()


def _markets(client, cfg) -> list[dict]:
    tc = cfg["trade"]
    if tc["markets"]:
        return client.markets(tickers=",".join(tc["markets"]))
    return [m for m in client.markets(status="open", mve_filter="exclude")
            if m.get("market_type") == "binary" and not m.get("mve_collection_ticker")][:50]


def _resting(client, ticker, oid):
    return next((o for o in client.resting_orders(ticker) if o["order_id"] == oid), None)


def _orders(client, con, d, cfg, clock, sleep):
    m = _markets(client, cfg)[0]
    t, ranges = m["ticker"], m.get("price_ranges") or []
    bid = cfg["trade"]["plumbing_bid"]
    up = round(bid + tick_size(bid, ranges), 4)
    oid = client.create_order(t, "bid", bid, 1, str(uuid.uuid4()), None)["order_id"]
    o = _resting(client, t, oid)
    if not o or abs(float(o["yes_price_dollars"]) - bid) > 1e-9:
        return False, f"placed order not resting at {bid} on {t}"
    oid = client.amend_order(oid, t, "bid", up, 1).get("order_id", oid)
    o = _resting(client, t, oid)
    if not o or abs(float(o["yes_price_dollars"]) - up) > 1e-9:
        return False, "price amend not reflected"
    oid = client.amend_order(oid, t, "bid", up, 2).get("order_id", oid)
    o = _resting(client, t, oid)
    if not o or abs(float(o["remaining_count_fp"]) - 2) > 1e-9:
        return False, "size amend not reflected"
    client.batch_cancel([oid])
    if _resting(client, t, oid):
        return False, "cancelled order still resting"
    return True, f"{t}: place, amend price, amend size, cancel confirmed"


def _group(client, con, d, cfg, clock, sleep):
    m = _markets(client, cfg)[0]
    t, ranges = m["ticker"], m.get("price_ranges") or []
    bid = cfg["trade"]["plumbing_bid"]
    gid = client.create_order_group(1)
    ids = [client.create_order(t, "bid", p, 1, str(uuid.uuid4()), gid)["order_id"]
           for p in (bid, round(bid - tick_size(bid, ranges), 4))]
    client.trigger_order_group(gid)
    left = [i for i in ids if _resting(client, t, i)]
    client.reset_order_group(gid)
    if left:
        client.batch_cancel(left)
        return False, f"trigger left {len(left)} order(s) resting"
    return True, f"{t}: group trigger cancelled both orders; reset"


def _fill(client, con, d, cfg, clock, sleep):
    start = clock()
    for m in _markets(client, cfg):
        t = m["ticker"]
        no = [(float(p), float(s)) for p, s in (client.orderbook(t).get("no_dollars") or [])
              if float(s) > 0]
        if not no:
            continue
        ask = round(1 - max(p for p, _ in no), 4)        # best YES ask
        before = {p["ticker"]: float(p["position_fp"]) for p in client.positions()}.get(t, 0.0)
        client.take(t, "bid", ask, 1)

        def landed():
            got = any(f["ticker"] == t for f in client.fills(min_ts=int(start) - 60))
            now = {p["ticker"]: float(p["position_fp"]) for p in client.positions()}.get(t, 0.0)
            return got and abs(now - before - 1) < 0.005
        if _wait(landed, clock, sleep, 60):
            return True, f"{t}: bought 1 at {ask}; fill and position confirmed"
        return False, f"{t}: fill or position not confirmed within 60 s"
    return False, "no demo market with a resting ask to trade against"


def _kill(client, con, d, cfg, clock, sleep):
    if not client.resting_orders():
        return False, "nothing was resting, so a stop proves nothing: let the trader quote first"
    (d / "KILL").write_text("drill\n", encoding="utf-8")
    ok = _wait(lambda: not (d / "KILL").exists() and not client.resting_orders(),
               clock, sleep, 120)
    return ok, "loop stopped and no orders rest" if ok else "loop did not stop clean in 120 s"


def _fresh(con, clock) -> bool:
    hb = tradedb.kv_get(con, "heartbeat")
    return hb is not None and clock() - float(hb) < 30


def _halt(client, con, d, cfg, clock, sleep):
    if not client.resting_orders():
        return False, "nothing was resting, so a halt proves nothing: let the trader quote first"
    (d / "HALT").write_text("drill\n", encoding="utf-8")
    try:
        ok = _wait(lambda: not client.resting_orders() and _fresh(con, clock), clock, sleep, 120)
    finally:
        (d / "HALT").unlink(missing_ok=True)
    back = ok and _wait(lambda: bool(client.resting_orders()), clock, sleep, 120)
    return back, ("orders cancelled with the loop alive; quoting resumed" if back
                  else "halt not honoured" if not ok else "quotes did not return after HALT")


def _fault(fault, client, con, d, cfg, clock, sleep):
    f = d / fault
    t0 = clock()
    f.write_text("drill\n", encoding="utf-8")
    try:
        _wait(lambda: False, clock, sleep, FAULT_S)
    finally:
        f.unlink(missing_ok=True)              # never leave the trader cut off
    t_off = clock()
    outages = con.execute("SELECT COUNT(*) FROM events WHERE kind='outage' AND ts BETWEEN ? AND ?",
                          (t0, t_off)).fetchone()[0]
    if not outages:
        return False, "no outage was recorded: was the trader running during the drill?"
    recovered = _wait(lambda: con.execute(
        "SELECT 1 FROM events WHERE kind='reconcile_ok' AND ts > ?", (t_off,)).fetchone(),
        clock, sleep, cfg["trade"]["reconcile_s"] + 120)
    errors = con.execute("SELECT COUNT(*) FROM events WHERE kind='error' AND ts >= ?",
                         (t0,)).fetchone()[0]
    ok = bool(recovered) and errors == 0 and _fresh(con, clock)
    return ok, (f"survived {FAULT_S} s of {fault}; reconciled clean after" if ok
                else f"recovered={bool(recovered)} errors={errors}")


def _reconcile(client, con, d, cfg, clock, sleep):
    (d / "FAULT_PHANTOM").write_text("drill\n", encoding="utf-8")
    try:
        halted = _wait(lambda: (d / "HALT").exists()
                       and "position" in (d / "HALT").read_text(encoding="utf-8"),
                       clock, sleep, cfg["trade"]["reconcile_s"] + 60)
    finally:
        (d / "FAULT_PHANTOM").unlink(missing_ok=True)
        (d / "HALT").unlink(missing_ok=True)
    return halted, ("a phantom position halted the loop; cleared" if halted
                    else "mismatch did not halt within the reconcile window")


DRILLS = {"orders": _orders, "group": _group, "fill": _fill, "kill": _kill, "halt": _halt,
          "network": functools.partial(_fault, "FAULT_NET"),
          "ratelimit": functools.partial(_fault, "FAULT_429"), "reconcile": _reconcile}


def run_drill(name: str, client, con, root: Path, cfg: dict, clock, sleep) -> tuple[bool, str]:
    d = Path(root) / "out" / "trade"
    d.mkdir(parents=True, exist_ok=True)
    try:
        ok, detail = DRILLS[name](client, con, d, cfg, clock, sleep)
    except Exception as e:  # noqa: BLE001 — a drill that crashes has failed
        ok, detail = False, f"{type(e).__name__}: {e}"
    tradedb.drill_result(con, clock(), name, ok, detail)
    tradedb.event(con, clock(), "drill", f"{name}: {'PASS' if ok else 'FAIL'} — {detail}")
    return ok, detail

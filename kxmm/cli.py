"""kxmm control: start | stop | status | log | test | run | sim  (Windows and macOS).

No PIDs or signals: the DB heartbeat says whether the loop is alive, and
`out/KILL` asks it to stop (Windows can't SIGTERM a console process). The loop
deletes KILL when it exits, which is how `stop` knows it finished.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .config import load_config
from .health import loop_state, report, stale_after_s

STOP_WAIT_S = 120
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
_sleep = time.sleep


def spawn_kwargs(platform: str) -> dict:
    if platform == "win32":
        return {"creationflags": DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def spawn_command(platform: str, executable: str, log_path: Path,
                  module: str = "kxmm.loop") -> list[str]:
    """The background loop's command line. On Windows prefer pythonw.exe: it
    has no console window that someone could close and kill the recorder with."""
    exe = executable
    if platform == "win32":
        w = Path(executable).with_name("pythonw.exe")
        if w.exists():
            exe = str(w)
    return [exe, "-u", "-m", module, "--log-file", str(log_path)]


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def cmd_start(root: Path, cfg: dict) -> None:
    con = db.connect(root / cfg["output"]["db_path"])
    state, age = loop_state(con, time.time(), stale_after_s(cfg))
    if state == "RUNNING":
        print(f"already running (heartbeat {age:.0f}s ago)")
        return
    (root / "out" / "KILL").unlink(missing_ok=True)      # a stale KILL would stop it at once
    log_path = root / cfg["output"]["log_path"]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(spawn_command(sys.platform, sys.executable, log_path), cwd=root,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, env={**os.environ, "PYTHONUTF8": "1"},
                     **spawn_kwargs(sys.platform))
    print(f"recorder started; logs: {log_path} (status shows RUNNING within ~2 min)")


def cmd_stop(root: Path, cfg: dict) -> None:
    _stop(root / "out" / "KILL")


def _stop(kill: Path) -> None:
    kill.parent.mkdir(parents=True, exist_ok=True)
    kill.write_text("stop\n", encoding="utf-8")
    deadline = time.monotonic() + STOP_WAIT_S
    while time.monotonic() < deadline:
        if not kill.exists():
            print("stopped")
            return
        _sleep(min(1.0, STOP_WAIT_S))
    # leave KILL: a busy loop stops after its current request; start clears it
    print("still stopping: the loop exits after its current request; run status")


def cmd_status(root: Path, cfg: dict) -> None:
    con = db.connect(root / cfg["output"]["db_path"])
    now = time.time()
    state, age = loop_state(con, now, stale_after_s(cfg))
    print(f"loop: {state}" + (f" (heartbeat {age:.0f}s ago)" if age is not None else ""))
    r = report(con, now, gap_threshold_s=cfg["record"]["gap_threshold_s"])
    print(f"universe: {r['universe']} markets | last 24h: {r['books_window']} book changes, "
          f"{r['trades_window']} trades")
    print(f"clean markets (24h, no gap > {cfg['record']['gap_threshold_s']}s): "
          f"{len(r['clean_markets'])} | longest gap: {r['max_gap_s']:.0f}s")
    for g in r["loop_gaps"]:
        print(f"  loop down {_when(g['start_ts'])} -> {_when(g['end_ts'])}")
    print("PHASE 1: " + ("PASS" if r["phase1_pass"] else "NOT YET"))


def cmd_log(root: Path, cfg: dict) -> None:
    p = root / cfg["output"]["log_path"]
    if not p.exists():
        print("no log yet")
        return
    print("".join(p.read_text(encoding="utf-8", errors="replace").splitlines(True)[-40:]),
          end="")


def _utc_day(s: str, end: bool = False) -> float:
    ts = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    return ts + 86399.999 if end else ts


def cmd_sim(root: Path, cfg: dict, argv: list[str]) -> None:
    import argparse
    from .kalshi_public import KalshiPublic
    from .simrun import run
    ap = argparse.ArgumentParser(prog="kxmm sim", description="replay the recording; Gate A report")
    ap.add_argument("--db", default=str(root / cfg["output"]["db_path"]))
    ap.add_argument("--from", dest="t0", help="first UTC day, YYYY-MM-DD")
    ap.add_argument("--to", dest="t1", help="last UTC day, YYYY-MM-DD")
    ap.add_argument("--offline", action="store_true", help="skip settlement lookups")
    a = ap.parse_args(argv)
    api = cfg["api"]
    client = None if a.offline else KalshiPublic(api["base_url"], api["rate_per_sec"],
                                                 api.get("timeout_s", 20), api.get("max_attempts", 4))
    print(run(cfg, Path(a.db), root / "out", _utc_day(a.t0) if a.t0 else None,
              _utc_day(a.t1, end=True) if a.t1 else None, client=client))


# --- Phase 3: demo trader -------------------------------------------------------

def _trade_stale_after(cfg: dict) -> float:
    return stale_after_s({"api": cfg["api"], "record": {"book_poll_s": cfg["trade"]["book_poll_s"]}})


def cmd_trade_panic(root: Path, cfg: dict, client=None) -> None:
    """Cancel everything on the account now, then stop and halt the loop if it runs."""
    from .tradeloop import build_client
    tdir = root / "out" / "trade"
    tdir.mkdir(parents=True, exist_ok=True)
    (client or build_client(cfg, root)).cancel_all()
    (tdir / "HALT").write_text("panic\n", encoding="utf-8")
    (tdir / "KILL").write_text("panic\n", encoding="utf-8")
    print("all demo orders cancelled; HALT and KILL written")


def cmd_trade_status(root: Path, cfg: dict) -> None:
    from . import tradedb
    from .gateb import gate_b
    from .risk import Pnl
    tdir = root / "out" / "trade"
    con = tradedb.connect(tdir / "trade.db")
    now = time.time()
    state, age = loop_state(con, now, _trade_stale_after(cfg))
    print(f"trader: {state}" + (f" (heartbeat {age:.0f}s ago)" if age is not None else ""))
    if (tdir / "HALT").exists():
        print("HALT: " + (tdir / "HALT").read_text(encoding="utf-8").strip())
    counts = dict(con.execute("SELECT kind, COUNT(*) FROM events WHERE ts >= ? GROUP BY kind",
                              (now - 86400,)).fetchall())
    fills = con.execute("SELECT COUNT(*) FROM fills WHERE ts >= ?", (now - 86400,)).fetchone()[0]
    print(f"last 24 h: {fills} fills, {counts.get('error', 0)} errors, "
          f"{counts.get('outage', 0)} outages, {counts.get('halt', 0)} halts, reconciles "
          f"{counts.get('reconcile_ok', 0)} ok / {counts.get('reconcile_bad', 0)} mismatched")
    pnl = Pnl()
    for r in con.execute("SELECT * FROM fills ORDER BY ts"):
        pnl.apply_fill(r["ticker"], r["book_side"], r["yes_price"], r["count"], r["fee"])
    pos = {t: p for t, p in pnl.positions().items() if abs(p) > 1e-9}
    print("positions: " + (", ".join(f"{t} {p:+g}" for t, p in sorted(pos.items())) or "flat"))
    latest = {r["name"]: r for r in con.execute("SELECT * FROM drills ORDER BY ts")}
    if latest:
        print("drills: " + ", ".join(f"{n} {'PASS' if r['passed'] else 'FAIL'}"
                                     for n, r in sorted(latest.items())))
    ok, reasons = gate_b(con, now, cfg["trade"])
    print("GATE B: PASS" if ok else "GATE B: NOT YET — " + "; ".join(reasons))


def cmd_trade(root: Path, cfg: dict, argv: list[str]) -> int:
    from . import tradedb
    from .drills import DRILLS, DRILLS_NEEDING_LOOP, run_drill
    from .tradeloop import KEY_ID, KEY_PATH, build_client, demo_credentials
    tdir = root / "out" / "trade"
    sub = argv[0] if argv else ""
    if sub == "start":
        key_id, key_path = demo_credentials(root)
        if not key_id or not key_path:
            print(f"missing {KEY_ID if not key_id else KEY_PATH}: create a Kalshi DEMO API key "
                  "and set it in .env (see README, Phase 3)")
            return 1
        con = tradedb.connect(tdir / "trade.db")
        if loop_state(con, time.time(), _trade_stale_after(cfg))[0] == "RUNNING":
            print("trader already running")
            return 0
        (tdir / "KILL").unlink(missing_ok=True)
        subprocess.Popen(spawn_command(sys.platform, sys.executable, tdir / "trade.log",
                                       module="kxmm.tradeloop"), cwd=root,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, env={**os.environ, "PYTHONUTF8": "1"},
                         **spawn_kwargs(sys.platform))
        print(f"demo trader started; logs: {tdir / 'trade.log'}")
        return 0
    if sub == "stop":
        _stop(tdir / "KILL")
        return 0
    if sub == "status":
        cmd_trade_status(root, cfg)
        return 0
    if sub == "panic":
        cmd_trade_panic(root, cfg)
        return 0
    if sub == "drill" and len(argv) > 1 and argv[1] in DRILLS:
        name = argv[1]
        con = tradedb.connect(tdir / "trade.db")
        if name in DRILLS_NEEDING_LOOP and \
                loop_state(con, time.time(), _trade_stale_after(cfg))[0] != "RUNNING":
            print(f"drill {name} needs the trader running: python kxmm.py trade start")
            return 1
        ok, detail = run_drill(name, build_client(cfg, root), con, root, cfg, time.time, time.sleep)
        print(f"drill {name}: {'PASS' if ok else 'FAIL'} — {detail}")
        return 0 if ok else 1
    print("usage: kxmm trade start|stop|status|panic|drill <" + "|".join(DRILLS) + ">")
    return 2


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    root = Path(root or Path(__file__).resolve().parent.parent)
    cmd = argv[0] if argv else ""
    if cmd == "test":
        return subprocess.call([sys.executable, "-m", "pytest", "-q"], cwd=root)
    if cmd == "run":
        from .loop import main as loop_main
        loop_main(["--config", str(root / "config.yaml")])
        return 0
    if cmd == "sim":
        cmd_sim(root, load_config(root / "config.yaml"), argv[1:])
        return 0
    if cmd == "trade":
        return cmd_trade(root, load_config(root / "config.yaml"), argv[1:])
    handlers = {"start": cmd_start, "stop": cmd_stop, "status": cmd_status, "log": cmd_log}
    if cmd not in handlers:
        print("usage: kxmm start|stop|status|log|test|run|sim|trade")
        return 2
    handlers[cmd](root, load_config(root / "config.yaml"))
    return 0

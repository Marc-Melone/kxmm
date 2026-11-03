"""Entry point: python -m kxmm.tradeloop [--config config.yaml] [--log-file …]  (DEMO ONLY)"""
from __future__ import annotations

import argparse
import logging
import os
import time
from pathlib import Path

from . import tradedb
from .auth import Signer, load_env
from .config import load_config
from .kalshi_trade import KalshiTrade
from .loop import configure_logging
from .trader import Trader

KEY_ID, KEY_PATH = "KALSHI_DEMO_KEY_ID", "KALSHI_DEMO_KEY_PATH"


def demo_credentials(root: Path) -> tuple[str | None, str | None]:
    env = {**load_env(root / ".env"), **os.environ}
    return env.get(KEY_ID) or None, env.get(KEY_PATH) or None


def load_signer(root: Path) -> Signer:
    """The demo key, or a SystemExit that names the setting, never the key's path."""
    key_id, key_path = demo_credentials(root)
    if not key_id or not key_path:
        raise SystemExit(f"missing {KEY_ID if not key_id else KEY_PATH}: see README, Phase 3")
    try:
        return Signer.from_pem(key_id, Path(key_path))
    except OSError as e:
        raise SystemExit(f"{KEY_PATH}: the key file could not be read "
                         f"({e.strerror or type(e).__name__}); see README, Phase 3") from None
    except (ValueError, TypeError):
        raise SystemExit(f"{KEY_PATH}: the file is not a Kalshi private key (.pem); "
                         "see README, Phase 3") from None


def build_client(cfg: dict, root: Path) -> KalshiTrade:
    return KalshiTrade(cfg["trade"]["base_url"], load_signer(root),
                       fault_dir=root / "out" / "trade")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="kxmm demo trader loop")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--log-file")
    args = ap.parse_args(argv)
    root = Path(args.config).resolve().parent
    cfg = load_config(args.config)
    configure_logging(Path(args.log_file) if args.log_file else None)
    con = tradedb.connect(root / "out" / "trade" / "trade.db")
    logging.getLogger("kxmm").info("demo trader starting (stop with out/trade/KILL)")
    try:
        Trader(cfg, build_client(cfg, root), con, root).run()
    except Exception as e:                     # e.g. a 401 at start: leave a trace for status
        tradedb.event(con, time.time(), "error", f"trader stopped: {type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    main()

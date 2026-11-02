"""Entry point: python -m kxmm.tradeloop [--config config.yaml] [--log-file …]  (DEMO ONLY)"""
from __future__ import annotations

import argparse
import logging
import os
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


def build_client(cfg: dict, root: Path) -> KalshiTrade:
    key_id, key_path = demo_credentials(root)
    if not key_id or not key_path:
        raise SystemExit(f"missing {KEY_ID if not key_id else KEY_PATH}: see README, Phase 3")
    return KalshiTrade(cfg["trade"]["base_url"], Signer.from_pem(key_id, Path(key_path)),
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
    Trader(cfg, build_client(cfg, root), con, root).run()


if __name__ == "__main__":
    main()

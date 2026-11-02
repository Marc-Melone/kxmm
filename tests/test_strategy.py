import re
from pathlib import Path

from kxmm.config import load_config


def test_sim_config_values():
    s = load_config()["sim"]
    assert s["strategy_version"] == "1.1.0"
    assert s["quote_size"] == 10 and s["stake_usd"] == 300
    assert s["max_loss_per_market_frac"] == 0.05
    assert s["gamma"] == 0.1 and s["vol_halflife_s"] == 3600 and s["horizon_s"] == 86400
    assert s["blackout_before_s"] == 259200
    assert s["reward_haircut"] == 0.5
    assert s["markouts_s"] == [60, 600, 3600] and s["primary_markout_s"] == 600
    assert s["bootstrap_n"] == 10000 and s["bootstrap_seed"] == 7
    assert "gate_a_min_markets" not in s                 # 1.1.0: reported, no longer decides
    assert s["gate_a_min_days"] == 10 and s["gate_a_min_hours_per_day"] == 20
    assert s["gate_a_min_fills"] == 100
    assert s["live_markets"] == 5
    assert s["book_lag_s"] == 2


def test_strategy_md_version_matches_config():
    # void policy: a parameter change must bump both, or this fails
    first = Path("STRATEGY.md").read_text(encoding="utf-8").splitlines()[0]
    m = re.search(r"Strategy version: (\S+)", first)
    assert m and m.group(1) == load_config()["sim"]["strategy_version"]

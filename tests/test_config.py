import pytest

from kxmm.config import load_config


def test_load_config_reads_values():
    assert load_config("config.yaml")["record"]["gap_threshold_s"] == 300


def test_load_config_names_missing_key(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "api: {base_url: x, rate_per_sec: 8}\n"
        "record: {book_poll_s: 10, trades_poll_s: 30, universe_refresh_s: 3600,"
        " gap_threshold_s: 300}\n"
        "output: {db_path: out/kxmm.db, log_path: out/kxmm.log}\n",
        encoding="utf-8")
    with pytest.raises(ValueError, match=r"record\.max_markets"):
        load_config(cfg)

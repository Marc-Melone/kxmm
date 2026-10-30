import shutil
from pathlib import Path

from kxmm import cli


def test_spawn_kwargs_per_platform():
    assert cli.spawn_kwargs("win32") == {"creationflags": 0x00000008 | 0x00000200}
    assert cli.spawn_kwargs("darwin") == {"start_new_session": True}
    assert cli.spawn_kwargs("linux") == {"start_new_session": True}


def test_stop_creates_kill_and_times_out_cleanly(tmp_path, monkeypatch, capsys):
    shutil.copy(Path("config.yaml"), tmp_path / "config.yaml")
    seen = []
    monkeypatch.setattr(cli, "STOP_WAIT_S", 0.2)
    monkeypatch.setattr(cli, "_sleep", lambda s: seen.append((tmp_path / "out" / "KILL").exists()))
    cli.main(["stop"], root=tmp_path)
    assert seen and all(seen)                       # KILL existed while waiting
    assert "did not confirm" in capsys.readouterr().out
    assert not (tmp_path / "out" / "KILL").exists()

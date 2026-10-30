import shutil
from pathlib import Path

from kxmm import cli


def test_spawn_kwargs_per_platform():
    assert cli.spawn_kwargs("win32") == {"creationflags": 0x00000008 | 0x00000200}
    assert cli.spawn_kwargs("darwin") == {"start_new_session": True}
    assert cli.spawn_kwargs("linux") == {"start_new_session": True}


def test_stop_timeout_leaves_kill_for_the_loop(tmp_path, monkeypatch, capsys):
    shutil.copy(Path("config.yaml"), tmp_path / "config.yaml")
    seen = []
    monkeypatch.setattr(cli, "STOP_WAIT_S", 0.2)
    monkeypatch.setattr(cli, "_sleep", lambda s: seen.append((tmp_path / "out" / "KILL").exists()))
    cli.main(["stop"], root=tmp_path)
    assert seen and all(seen)                       # KILL existed while waiting
    assert "still stopping" in capsys.readouterr().out
    # the request stands: a busy loop stops after its current request, and
    # `start` clears a stale KILL itself
    assert (tmp_path / "out" / "KILL").exists()


def test_spawn_command_prefers_pythonw_on_windows(tmp_path):
    exe, exew = tmp_path / "python.exe", tmp_path / "pythonw.exe"
    exe.write_text(""), exew.write_text("")
    log = tmp_path / "out" / "kxmm.log"
    assert cli.spawn_command("win32", str(exe), log) == [
        str(exew), "-u", "-m", "kxmm.loop", "--log-file", str(log)]
    exew.unlink()                                  # no pythonw: fall back to python.exe
    assert cli.spawn_command("win32", str(exe), log)[0] == str(exe)
    assert cli.spawn_command("darwin", "/v/bin/python", log)[0] == "/v/bin/python"

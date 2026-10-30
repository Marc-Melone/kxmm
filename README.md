# kxmm: Kalshi market-making test

Phase 1 is a **recorder**. It picks up to 40 Kalshi markets that pay liquidity rewards and records their order books (every 10 s) and trades (every 30 s) into `out/kxmm.db`. It uses public data only: no account, no API key, no orders. The design and the full test plan are in `DESIGN.md`.

**Phase 1 passes** when `status` prints `PHASE 1: PASS`: 24 hours on 20+ markets with no recording gap over 5 minutes.

## Commands

| Windows | macOS | What it does |
|---|---|---|
| `.\kxmm.ps1 start` | `./kxmm.sh start` | Start the recorder in the background |
| `.\kxmm.ps1 status` | `./kxmm.sh status` | Running or stopped, markets, gaps, Phase 1 verdict |
| `.\kxmm.ps1 stop` | `./kxmm.sh stop` | Ask it to stop (it finishes its current request first; can take up to 2 min) |
| `.\kxmm.ps1 log` | `./kxmm.sh log` | Last 40 log lines |
| `.\kxmm.ps1 test` | `./kxmm.sh test` | Run the test suite |
| `.\kxmm.ps1 run` | `./kxmm.sh run` | Run in the foreground (Ctrl+C to quit) |

**Never end it from Task Manager.** Use `stop`. Phase 1 places no orders, so nothing is at risk yet, but later phases rely on the same habit.

**One machine at a time.** Don't run it on the Mac and on Windows at once.

## Windows setup (once)

1. **Install Python 3.12** from python.org and tick "Add python.exe to PATH".
2. **Copy this folder** to the Windows machine, e.g. `C:\Users\<you>\Projects\kxmm`. Leave out `.venv`, `out` and `.env`; the venv is platform-specific and gets rebuilt.
3. In PowerShell, inside the folder:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
   ```
4. **Never sleep.** The recorder also asks Windows to stay awake while it runs, but set the power plan too:
   ```powershell
   powercfg /change standby-timeout-ac 0
   powercfg /change hibernate-timeout-ac 0
   ```
   On a laptop, keep it plugged in and stop a closed lid from sleeping it:
   ```powershell
   powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
   powercfg /setactive SCHEME_CURRENT
   ```
5. **Check and start:**
   ```powershell
   .\kxmm.ps1 test      # all tests pass
   .\kxmm.ps1 start
   .\kxmm.ps1 status    # loop: RUNNING within ~2 minutes
   ```
6. **Prove it survives on this machine** before trusting the 24-hour clock. This launch path has only been tested on a Mac:
   1. After `start`, **close the PowerShell window**.
   2. Open a new one, `cd` back into the folder, and wait 2 minutes.
   3. `.\kxmm.ps1 status` must say `loop: RUNNING`, and `.\kxmm.ps1 log` must show "universe refreshed".
   4. `.\kxmm.ps1 stop` must print `stopped`, and `status` must then say `STOPPED`.
   5. `.\kxmm.ps1 start` again and leave it running.

   If step 3 says STOPPED, closing the window killed the recorder. Tell Claude; it can switch the launch to a scheduled task.

After a reboot (Windows Update does this on its own; set active hours or pause updates for the test), run `.\kxmm.ps1 start` again. The downtime shows up as a gap, and the 24-hour Phase 1 clock restarts from there.

## Layout

```
config.yaml        every knob, commented
DESIGN.md          the spec: the test, phases, gates, timeline
kxmm/              recorder, Kalshi client, selector, reward math, ledger
kxmm.py            start/stop/status/log/test/run (kxmm.ps1, kxmm.sh wrap it)
out/kxmm.db        SQLite: programs, markets, selections, books, trades, gaps
out/kxmm.log       loop log (a summary line every 10 minutes)
```

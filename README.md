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

## Phase 2: simulate (Gate A on Nov 16)

`sim` replays the recording through the pre-registered strategy in `STRATEGY.md` and prints the Gate A verdict. It only **reads** the recorder's database, so it is safe to run while the recorder is running:

```powershell
.\kxmm.ps1 sim                    # whole recording, with settlement lookups
.\kxmm.ps1 sim --from 2026-11-04  # from a UTC day on
.\kxmm.ps1 sim --offline          # no network: settlements left unknown
```

How to read the report:
- **Per market:** simulated net for each recorded market (reported only; rewards make most of them positive).
- **Daily totals:** the agreed test. `beyond chance: YES` means the 97.5% lower bound of the mean daily P&L is above zero. "trading only" shows the result without rewards.
- **LIVE TARGET:** the fills needed for t ≈ 2 at the simulated edge, and about how many days 5 live markets take to get them.
- **GATE A** (STRATEGY.md 1.1.0): needs 10 days with 20+ hours recorded, 100 fills with a known 10-minute markout, a total beyond chance, and a positive trading edge.
  - `PASS`: all four hold.
  - `PASS — rewards only`: everything but the trading edge, so live trading stays small until Kalshi renews its rewards.
  - `NOT YET`: printed with the reasons.

Results are also saved to `out\sim.db` and `out\sim_market_days.csv`. To run it on the Mac instead, copy `out\kxmm.db` across, ideally while the recorder is stopped, and run `./kxmm.sh sim --db <copy>`.

## Phase 3: demo trading (Gate B by Nov 24)

The trader runs the quoting rules on Kalshi's **demo exchange** (mock money). It cannot reach the real exchange: the code refuses any non-demo address. Run it on the Windows machine next to the recorder; it uses its own folder, `out\trade\`.

**One-time setup:**
1. Make a demo account at https://demo.kalshi.co (it is separate from your real Kalshi login).
2. In the demo site's account settings, create an **API key** (choose **Ed25519** if asked) on the Windows machine. Save the downloaded `.pem` **outside** the kxmm folder, e.g. `C:\Users\<you>\keys\kalshi-demo.pem`.
3. Copy `.env.example` to `.env` in the kxmm folder and fill in both lines: the key ID shown by Kalshi and the path to the `.pem`. Then lock the file down:
   ```powershell
   icacls .env /inheritance:r /grant:r "$env:USERNAME:(R,W)"
   ```
   Never paste the key or its ID into a chat.

**Commands:**

| Command | What it does |
|---|---|
| `.\kxmm.ps1 trade start` | Start the demo trader (cancels any open demo orders first) |
| `.\kxmm.ps1 trade status` | Running or stopped, last 24 h, positions, drills, **GATE B** |
| `.\kxmm.ps1 trade stop` | Stop: cancel all demo orders and exit |
| `.\kxmm.ps1 trade panic` | Cancel all demo orders right now, even if the trader has died |
| `.\kxmm.ps1 trade drill <name>` | Run one safety drill (list below) |

**Drills.** Gate B needs each one to pass at least once within the 5 clean days. Run them in this order:
1. Without the trader running:
   - `orders`: place, amend and cancel.
   - `group`: an order-group trigger cancels the group's orders.
   - `fill`: buys 1 contract from someone else's demo order and checks the fill and position.
2. Run `trade start`, wait 2 minutes, then:
   - `halt`
   - `network`: 60 s of simulated network loss.
   - `ratelimit`: 60 s of simulated rate limiting.
   - `reconcile`: a fake position mismatch must halt the trader.
   - `kill`: stops the trader. Run `trade start` again afterwards.

**Gate B** passes after 5 consecutive UTC days, each with 20+ hours of trader uptime and no errors or reconcile mismatches, with every drill passed in that window. Network and rate-limit outages count as handled, not as errors.

## Layout

```
config.yaml        every knob, commented
DESIGN.md          the spec: the test, phases, gates, timeline
kxmm/              recorder, Kalshi client, selector, reward math, ledger
kxmm.py            start/stop/status/log/test/run (kxmm.ps1, kxmm.sh wrap it)
out/kxmm.db        SQLite: programs, markets, selections, books, trades, gaps
out/kxmm.log       loop log (a summary line every 10 minutes)
```

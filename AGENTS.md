# kxmm: context for AI agents

Read this before doing anything in this repo. It is written for an agent running on the **Windows machine** that hosts the 24/7 recorder and the demo trader, but it applies anywhere.

**kxmm** tests whether a small, maker-only market-making bot on Kalshi makes money **consistently enough that it isn't luck**. It is built in gated phases. Each gate is a pre-registered verdict, and nothing moves on until a gate passes.

## Where things stand (2026-11-02)

| Phase | What | State |
|---|---|---|
| 1. Recorder | Records books and trades for up to 40 Kalshi reward markets, using public data only | Built and reviewed. **Windows deployment and the survival check below are not confirmed yet.** |
| 2. Simulator | Replays the recording through the strategy and gives the Gate A verdict | Built and reviewed. Waits on 10 recorded days. |
| 3. Demo trader | Real orders on Kalshi's **demo** exchange (mock money), with drills and the Gate B verdict | Built and reviewed. **Never run against demo yet**: needs the user's demo key on this machine. |
| 4. Live, small | Real money at small size | **Not built.** Do not build it unless the user asks, and only through a reviewed plan. |

| Gate | Date | Passes when |
|---|---|---|
| Phase 1 | 24 h after the recorder starts | `.\kxmm.ps1 status` says `PHASE 1: PASS`: 24 h on 20+ markets (books and trades), with no gap over 5 min |
| A | Nov 16 | `.\kxmm.ps1 sim` says `GATE A: PASS` or `PASS — rewards only` (rules in STRATEGY.md 1.1.0) |
| B | Nov 24 | `.\kxmm.ps1 trade status` says `GATE B: PASS`: 5 clean UTC days on demo, every drill passed |
| C | Dec 28 | The live go/no-go. Phase 4, not built |

The plan, with its timeline and risk limits, lives in the user's "Prediction-Market Market-Making Plan" doc on claude.ai. In this repo, `DESIGN.md` is the spec and `STRATEGY.md` is the pre-registration.

## Rules that are never broken

1. **No real money.** `KalshiTrade` refuses any base URL except the two demo URLs, and that refusal stays. Never add a production URL or key path, and never write Phase 4 code unless the user asks for it. Arming live trading is always the user's own action.
2. **Credentials belong to the user.** The user creates Kalshi keys, puts the `.pem` outside this folder, and fills in `.env`.
   - Never ask for, paste, print, log or commit a key, key id or key path.
   - Never read the `.pem`. Code may load it through `kxmm.tradeloop.load_signer`, and nothing else.
   - `.env`, `*.pem`, `out/` and `.venv/` are never committed (`.gitignore` covers them; keep it that way).
3. **One machine.** Only this Windows box runs the recorder and the trader. The trader cancels every order on the demo account at start and stop, so a second copy anywhere would fight it.
4. **Stop with commands, never by killing processes.** Use `.\kxmm.ps1 stop` and `.\kxmm.ps1 trade stop`, never Task Manager or `Stop-Process`. A killed trader leaves orders resting until they expire (≤ 5 min). If it is wedged, run `.\kxmm.ps1 trade panic`.
5. **The pre-registration is locked.**
   - `STRATEGY.md` and the `sim:` section of `config.yaml` change together, with a version bump and a Changelog line; `tests/test_strategy.py` enforces this.
   - Tuning may use only the first 5 recorded days.
   - Never change a rule, threshold or parameter to make a gate pass.
6. **Gate rules are the user's.** Changing Gate A, Gate B or the agreed test (STRATEGY.md "Metrics and the test") needs the user's explicit decision.
7. **Ask the user first** before any of these:
   - pushing to GitHub or merging to `main`
   - deleting or rewriting anything in `out/` (the recording is irreplaceable)
   - stopping a long run the gates depend on
   - changing Windows power, clock or security settings
   - any action on the Kalshi account beyond drills the user asked for

## Windows setup (once)

1. Install **Python 3.12** from python.org, ticking "Add python.exe to PATH". Also install **Git for Windows**.
2. Clone the repo (it's private; Git opens a browser window for GitHub sign-in) and build the venv:
   ```powershell
   cd $HOME
   git clone https://github.com/Marc-Melone/kxmm.git
   cd kxmm
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
   ```
3. Never sleep. These are system settings: **the user runs them, or approves them first.**
   ```powershell
   powercfg /change standby-timeout-ac 0
   powercfg /change hibernate-timeout-ac 0
   powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
   powercfg /setactive SCHEME_CURRENT
   ```
4. Run `.\kxmm.ps1 test`. Every test should pass (191 as of 2026-11-02).
5. Updating later:
   ```powershell
   .\kxmm.ps1 trade stop
   .\kxmm.ps1 stop
   git pull
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   .\kxmm.ps1 start
   .\kxmm.ps1 trade start
   ```
   Start the trader only once it's set up; see "Still owed" below.

## Commands

| Command | What it does | Healthy looks like |
|---|---|---|
| `.\kxmm.ps1 start` / `stop` / `status` / `log` | Phase 1 recorder | `loop: RUNNING`, a universe of 40 markets, and the clean-market count climbing toward `PHASE 1: PASS` |
| `.\kxmm.ps1 sim [--from YYYY-MM-DD] [--offline]` | Gate A report; reads the recorder DB **read-only**, safe while recording | `GATE A: NOT YET — …` with reasons until Nov 16 |
| `.\kxmm.ps1 trade start` / `stop` / `status` / `panic` | Demo trader | `trader: RUNNING`, 0 errors, reconciles ok, positions small |
| `.\kxmm.ps1 trade drill <name>` | One safety drill on demo | `drill <name>: PASS — …` |
| `.\kxmm.ps1 test` | The full test suite | Every test passes |

The recorder and the trader are separate processes. The recorder uses `out\` (`kxmm.db`, `kxmm.log`, `KILL`), and the trader uses `out\trade\` (`trade.db`, `trade.log`, `KILL`, `HALT`).

## Still owed on this machine (in this order)

1. **Recorder survival check.** Run `.\kxmm.ps1 start`, then **close the PowerShell window**. Open a new one and wait 2 minutes.
   - `.\kxmm.ps1 status` must say RUNNING, and the log must show "universe refreshed".
   - If it says STOPPED, closing the window killed the recorder: report it; the fix is launching via a scheduled task.
2. **Demo key** (user only). The user makes an account at demo.kalshi.co, creates an **Ed25519** API key, saves the `.pem` outside the repo, and copies `.env.example` to `.env` in Notepad. Then lock `.env` down:
   ```powershell
   icacls .env /inheritance:r /grant:r "${env:USERNAME}:(R,W)"
   ```
3. **Drills, in this order:**
   1. With the trader stopped: `orders`, `group`, `fill`.
   2. Run `trade start`, then wait 2 minutes.
   3. `halt`, `network`, `ratelimit`, `reconcile`, `kill`.
   4. Run `trade start` again and leave it running.

   These are the **first real calls to Kalshi's trading API**: only fakes have been tested so far. For any FAIL, capture the exact output line and the last 40 lines of `out\trade\trade.log`. Diagnose before retrying, and never retry in a loop.
4. **Daily:** check `.\kxmm.ps1 status` and `.\kxmm.ps1 trade status`, and report anything that isn't healthy.

## Development workflow

- **Test first.** Write the failing test, watch it fail, make it pass, then run the whole suite. Fakes for Kalshi live in `tests/fakes.py` (the trading exchange) and `tests/helpers.py` (books and markets). When real Kalshi behaviour differs from a fake, fix the fake too.
- **Plans live in `docs/superpowers/plans/`.** There is one per phase, and each records the decisions behind the code. A review-driven change records its reasoning in the commit message.
- **Every knob goes in `config.yaml`,** with a comment. Code reads config, not literals. Dependencies are pinned in `requirements.txt`.
- **Windows rules:**
  - Use `pathlib` paths and `encoding="utf-8"` on every file open.
  - No signals and no `fork`. Stops go through KILL files and liveness through the DB heartbeat.
  - Background launches use `pythonw.exe` with `--log-file` (see `kxmm/cli.py`).
- **Never open `out\kxmm.db` for writing from tools.** The simulator uses `replay.open_readonly`. To analyse data elsewhere, copy the DB while the recorder is stopped, or read it read-only.
- **Git:** work on a branch and keep commits small, with conventional messages (`feat:`, `fix:`, `docs:`). Merge and push only with the user's OK; there is no CI, so run the suite before every commit.

## Code map

| File | Role |
|---|---|
| `kxmm/cli.py` + `kxmm.py`, `kxmm.ps1` | Every command: recorder start/stop/status, `sim`, `trade …`, the Gate B status output |
| `kxmm/recorder.py`, `loop.py`, `select.py`, `db.py`, `health.py` | Phase 1: the loop, market selection (reward yield, sticky universe), the SQLite ledger with gap tracking, and the Phase 1 verdict |
| `kxmm/kalshi_public.py` | Public REST client: rate limit, backoff, `on_attempt` heartbeat hook |
| `kxmm/book.py`, `rewards.py`, `fair.py` | Order book (YES and NO bid ladders), Kalshi reward scoring, microprice and EWMA volatility |
| `kxmm/replay.py`, `quoting.py`, `fills.py`, `sim.py`, `stats.py`, `simrun.py` | Phase 2: read-only replay with merged gaps, the quoting rules, the strict-queue fill model, the simulator, the day bootstrap and Gate A, and the report |
| `kxmm/auth.py`, `kalshi_trade.py`, `orders.py`, `risk.py`, `trader.py`, `tradeloop.py`, `tradedb.py`, `drills.py`, `gateb.py` | Phase 3: the signer and `.env` loader, the demo-only client, quote-to-order diffing, P&L and risk rails, the trader loop, the trade ledger, drills, and Gate B |

## Kalshi facts (checked against docs.kalshi.com, Oct 30 to Nov 2, 2026)

- **Public data:** `https://api.elections.kalshi.com/trade-api/v2` serves markets, order books, trades and `incentive_programs`, with no key needed. The recorder stays at or under 8 requests/s.
- **Demo:** the REST API is at `https://external-api.demo.kalshi.co/trade-api/v2`. Demo books are mostly empty and there are no reward programs, so demo proves plumbing, not profit. The trader posts far-from-touch "plumbing quotes" on empty demo books.
- **Signing:**
  - The signed string is `str(timestamp_ms) + METHOD + path`, where the path includes `/trade-api/v2` and excludes the query string.
  - Ed25519 keys sign that string directly. RSA keys use PSS-SHA256.
  - A 401 usually means a bad key or **clock drift**.
- **Orders:** `POST /portfolio/events/orders`.
  - `side` is `bid` or `ask` **on the YES leg**: our NO bid at q is a YES ask at 1 − q.
  - Prices are dollar strings like `"0.4900"`; counts are 2-decimal strings.
  - Every order is `post_only` and `cancel_order_on_pause`, and expires after `order_ttl_s` (300 s) unless refreshed.
- **Amend:** `count` = already filled + desired resting. Changing the price or increasing the size loses queue position; changing only the expiry, or decreasing the size, keeps it.
- **Order groups:** when fills exceed the limit within 15 s, Kalshi cancels the group's orders and rejects new ones until the group is reset. There's no documented "triggered" flag, so the trader infers trips from rejections.
- **Rewards:** `period_reward` is in centi-cents and covers the whole program window.
  - The reference price is the first level, walking down from the best bid, where cumulative size reaches one fifth of the target size.
  - Each tick below the reference halves an order's score.
  - Only depth up to the target size scores.
  - A snapshot counts only when both sides meet the target.

## Known gaps (deferred from code reviews; fix only when asked)

- **Recorder:** an `orderbook` response with no `orderbook_fp` reads as an empty book; `stop` with nothing running waits 120 s; the book dedup assumes the clock only moves forward; trades are stored without `is_block_trade`; no server clock offset is recorded.
- **Simulator:** `--from` inside a gap has no starting book; sub-1-contract fills count as full fills in `fills_needed`; "inventory luck" is full trading P&L to settlement; the t critical value is not printed.
- **Trader:**
  - a flat position can overshoot the event cap by one lot
  - standalone drills aren't refused while the trader runs
  - `trade status` has no equity line
  - signed requests follow redirects
  - subaccount defaults differ between calls
  - every start creates a new order group
  - cancel-all can also cancel orders placed in the next minute

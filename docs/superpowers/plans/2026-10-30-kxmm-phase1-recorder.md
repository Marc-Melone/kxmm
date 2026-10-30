# kxmm Phase 1 (Recorder) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A key-free recorder that picks up to 40 Kalshi liquidity-reward markets, records their order books and trades 24/7 on a Windows machine, and reports whether Phase 1's pass rule holds (24 h on 20+ markets, no gap over 5 min).

**Architecture:** Pure modules (book parsing, reward-share math, market selection) feed a single-threaded loop that polls Kalshi's public REST API under a local rate limit and writes to SQLite. A cross-platform Python launcher (`kxmm.py`) starts, stops and inspects the loop: a heartbeat in the DB shows liveness, and a `out/KILL` file asks it to stop, so no signals or PID probing are needed.

**Tech Stack:** Python 3.12+ (3.13 on the Mac, 3.12 on Windows), requests, PyYAML, pytest, sqlite3 (stdlib).

**Spec:** `DESIGN.md` (sections "How it's built", "Reward and rate facts", "Phases, gates and timeline").

**Scope:** Phase 1 only. The simulator, quoting engine, report, dashboard (:5120) and the `gui` and `panic` commands come in the Phase 2 plan. The order client and live ledger come later still.

## Global Constraints

- Public, unauthenticated Kalshi endpoints only. No API keys, no orders, no `cryptography` dependency in this phase.
- Base URL `https://api.elections.kalshi.com/trade-api/v2`. Local cap of **8 requests/s**, bounded backoff on 429/5xx.
- Pinned deps: `requests==2.34.2`, `PyYAML==6.0.3`, `pytest==9.1.1`.
- Runs on Windows and macOS: `pathlib` everywhere, files opened with `encoding="utf-8"`, no `fork`, no SIGTERM handling, no shell-only features in Python.
- Every knob lives in `config.yaml` with an inline comment. Code reads values from config, never literals.
- SQLite in WAL mode at `out/kxmm.db`; all timestamps are epoch seconds (REAL, UTC).
- Prices and sizes arrive as strings (`"0.4500"`, `"15.37"`). Parse to `float` for math, and store the raw JSON for books.
- `period_reward` is in centi-cents and is the total for the program window: daily pool = `period_reward / 10_000 / program_days`.
- Reward scoring rules are exactly those in DESIGN.md "Reward and rate facts". The reference price uses one fifth of the target size.
- Selection rules:
  - Active liquidity program with ≥ 24 h left.
  - `market_type == "binary"`, no `mve_collection_ticker`, `status == "active"`.
  - Mid in [0.10, 0.90], and ≥ 3 days to `close_time`.
  - Both book sides have depth ≥ `target_size`.
  - At most 2 markets per event and at most 40 markets.
  - The universe is sticky: a market stays until it fails a filter.
  - Maker-fee series (`fee_type == "quadratic_with_maker_fees"`) are excluded unless the estimated reward per contract per day ≥ `0.0175 × mid × (1 − mid)`.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Network blips or a 429 storm mid-cycle** (common on home Wi-Fi). Expected: the loop logs, skips that market for the cycle, and keeps running. A gap is recorded only if a market goes more than 300 s without a good poll. Pinned in Task 6 (`test_poll_books_survives_one_failing_market`).
2. **A market settles or its program ends while recorded.** Expected: an orderbook 404 drops it from the universe without crashing, and the next refresh no longer selects it. Pinned in Task 6 (`test_poll_books_drops_market_on_not_found`).
3. **One-sided or empty books** (`yes_dollars: null`, `no_dollars: []`). Expected: parsing yields an empty side, mid is `None`, the selector skips the market, and the recorder still stores the book. Pinned in Task 3 (`test_book_handles_null_and_empty_sides`).
4. **The Windows machine sleeps, reboots, or the process is killed with End task.** Expected: `status` reads STOPPED (stale heartbeat), `start` works without manual cleanup, and on restart the downtime is written as gaps. Pinned in Task 4 (`test_restart_gaps_cover_downtime`) and Task 7 (`test_status_stale_heartbeat_is_stopped`).
5. **Busy markets with more trades per poll than one page, and overlapping polls.** Expected: pagination continues until the cursor is empty (capped at 20 pages with a warning), and a trade seen twice is stored once. Pinned in Task 2 (`test_trades_paginates_until_empty_cursor`) and Task 4 (`test_store_trades_dedupes_by_trade_id`).

---

### Task 1: Scaffold, config loader, keep-awake

**Files:**
- Create: `requirements.txt`, `pytest.ini`, `.gitignore`, `config.yaml`, `kxmm/__init__.py`, `kxmm/config.py`, `kxmm/keepawake.py`
- Test: `tests/test_config.py`, `tests/test_keepawake.py`

**Interfaces:**
- Produces: `load_config(path: str | Path = "config.yaml") -> dict`, which raises `ValueError` naming the first missing required key. Required keys are `api.base_url`, `api.rate_per_sec`, `record.book_poll_s`, `record.trades_poll_s`, `record.universe_refresh_s`, `record.max_markets`, `record.gap_threshold_s`, `output.db_path`, `output.log_path`.
- Produces: `keep_awake(platform: str | None = None, kernel32=None, popen=subprocess.Popen)`, a context manager. On `win32` it calls `kernel32.SetThreadExecutionState(0x80000001)` on enter and `(0x80000000)` on exit. On `darwin` it starts `["caffeinate", "-i", "-w", str(os.getpid())]` and terminates it on exit. Anything else is a no-op.

- [ ] **Step 1: Create the venv and pinned files.** `requirements.txt` holds the three pins from Global Constraints. `pytest.ini` holds `[pytest]` and `pythonpath = .`. `.gitignore` holds `.venv/`, `out/`, `__pycache__/`, `.pytest_cache/`, `.env`.
  Run: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`. Expected: installs without errors.

- [ ] **Step 2: Write `config.yaml` with exactly these values**

```yaml
# kxmm — every knob lives here. DESIGN.md is the spec.
api:
  base_url: https://api.elections.kalshi.com/trade-api/v2
  rate_per_sec: 8            # local cap; public limit undocumented (DESIGN.md)
  timeout_s: 20
  max_attempts: 4            # backoff 1,2,4,8 s on 429/5xx
  tickers_batch: 50          # tickers per GET /markets?tickers=
record:
  book_poll_s: 10            # order book per market
  trades_poll_s: 30          # trades per market
  universe_refresh_s: 3600   # re-run selection hourly
  trades_backfill_s: 3600    # first trades poll for a new market looks back this far
  trades_max_pages: 20
  max_markets: 40
  max_per_event: 2
  shortlist_n: 150           # markets whose books are fetched for scoring each refresh
  gap_threshold_s: 300       # Phase 1 pass: no gap over 5 min
select:
  mid_lo: 0.10
  mid_hi: 0.90
  min_days_to_close: 3
  min_program_hours_left: 24
  quote_size: 10             # contracts per side used to estimate reward share
  maker_fee_rate: 0.0175     # maker fee = rate * P * (1-P) per contract
output:
  db_path: out/kxmm.db
  log_path: out/kxmm.log
```

- [ ] **Step 3: Write failing tests**
  - `test_load_config_reads_values`: `load_config("config.yaml")["record"]["gap_threshold_s"] == 300`.
  - `test_load_config_names_missing_key`: a temp YAML without `record.max_markets` raises `ValueError` whose message contains `"record.max_markets"`.
  - `test_keep_awake_windows_sets_and_clears`: with a fake kernel32 that records calls, `platform="win32"` yields the calls `[0x80000001, 0x80000000]`.
  - `test_keep_awake_mac_starts_and_stops_caffeinate`: with a fake popen, `platform="darwin"` starts a process whose args begin `["caffeinate", "-i", "-w"]`, and `terminate()` is called on exit.
  - `test_keep_awake_other_is_noop`: with `platform="linux"`, the fakes are never called.

- [ ] **Step 4: Run them.** Run: `.venv/bin/pytest -q tests/test_config.py tests/test_keepawake.py`. Expected: FAIL (module not found).
- [ ] **Step 5: Implement `kxmm/config.py` and `kxmm/keepawake.py`** per the Interfaces.
- [ ] **Step 6: Run them.** Expected: 5 passed.
- [ ] **Step 7: Commit** `DESIGN.md`, `docs/`, `tests/fixtures/`, the scaffold and the tests with message `feat: scaffold kxmm with config loader and keep-awake`.

### Task 2: Kalshi public client

**Files:**
- Create: `kxmm/kalshi_public.py`, adapted from `~/Desktop/Project/kxsurv/src/kxsurv/api.py` (keep its `RateLimiter` and backoff; drop the historical/candlestick code)
- Test: `tests/test_kalshi_public.py` (uses `tests/fixtures/*.json`)

**Interfaces:**
- Produces: `class NotFound(Exception)` and `class Unavailable(RuntimeError)`.
- Produces: `KalshiPublic(base_url: str, rate_per_sec: float, timeout_s: float, max_attempts: int, session=None)`. `session` is injectable for tests. A `base_url` other than the configured production URL or `https://external-api.kalshi.com/trade-api/v2` raises `ValueError`. Methods:
  - `incentive_programs(status: str = "active", type_: str = "liquidity") -> list[dict]`: `limit=1000`, follows `next_cursor`.
  - `markets_by_tickers(tickers: list[str], batch: int) -> list[dict]`: `GET /markets?tickers=a,b,...`, in batches.
  - `series(ticker: str) -> dict`: the `"series"` object.
  - `orderbook(ticker: str) -> dict`: the `"orderbook_fp"` object. A 404 raises `NotFound`.
  - `trades(ticker: str, min_ts: int, max_pages: int) -> list[dict]`: `GET /markets/trades`, `limit=1000`, follows `cursor`, and stops on an empty cursor or an empty page. When it hits `max_pages` it logs a WARNING and returns what it has.
  - All calls go through `_get`, which retries 429/5xx with backoff `2**attempt` seconds (sleep injectable) and raises `Unavailable` after `max_attempts`.

- [ ] **Step 1: Write failing tests** with a fake session that serves queued `(status, json)` responses and records request URLs and params:
  - `test_incentive_programs_follows_next_cursor`: page 1 is the fixture plus a `next_cursor`, and page 2 has `[]` with no cursor. The result has 3 rows, and the second request carried `cursor=<next_cursor>`.
  - `test_markets_by_tickers_batches`: 120 tickers with `batch=50` make 3 requests, and the first `tickers` param has 50 comma-joined items.
  - `test_orderbook_returns_fp_object`: the fixture's `yes_dollars[-1] == ["0.9500", "44.00"]`.
  - `test_orderbook_404_raises_not_found`.
  - `test_retries_429_then_succeeds`: responses `[429, 200]` give a result, and the fake sleep was called once with `1`.
  - `test_gives_up_after_max_attempts`: four 503s raise `Unavailable`.
  - `test_trades_paginates_until_empty_cursor`: pages of 2, 2 and 1 rows with cursors `c1`, `c2` and `""` return 5 rows, and every request carried `min_ts`.
  - `test_trades_stops_at_max_pages`: endless cursors with `max_pages=3` make exactly 3 requests.
  - `test_rejects_unknown_base_url`.
- [ ] **Step 2: Run them.** Run: `.venv/bin/pytest -q tests/test_kalshi_public.py`. Expected: FAIL.
- [ ] **Step 3: Implement `kxmm/kalshi_public.py`** per the Interfaces.
- [ ] **Step 4: Run them.** Expected: 9 passed.
- [ ] **Step 5: Commit** with message `feat: Kalshi public client with rate limit and pagination`.

### Task 3: Book parsing and reward-share math (pure)

**Files:**
- Create: `kxmm/book.py`, `kxmm/rewards.py`
- Test: `tests/test_book.py`, `tests/test_rewards.py`

**Interfaces:**
- Produces: `Book` dataclass with `yes` and `no` as `list[tuple[float, float]]` ordered **best first** (price descending). Plus:
  - `Book.from_orderbook(raw: dict) -> Book`, where a `None` or missing side becomes `[]`.
  - Properties `best_yes_bid`, `best_no_bid` and `yes_ask` (= 1 − best_no_bid), each `float | None`.
  - Methods `mid() -> float | None` and `spread() -> float | None`.
- Produces: `book_digest(raw: dict) -> str`, a sha1 of `json.dumps(raw, sort_keys=True)`.
- Produces from `rewards.py`:
  - `tick_size(price: float, price_ranges: list[dict]) -> float`: the `step` of the range with `start <= price < end`. It falls back to the last range when price == 1.0.
  - `SideScore` dataclass with `ref_price: float | None`, `qualifying_score: float`, `depth: float` and `meets_target: bool`.
  - `side_score(levels: list[tuple[float, float]], target_size: float, discount: float, price_ranges: list[dict]) -> SideScore`. Levels are best first. The reference price is the first level where cumulative size ≥ target/5. Qualifying levels run from the best one up to and including the level where cumulative size first reaches the target. Each level scores `size × discount ** ticks_below_ref`, where ticks = `round((ref - price) / tick_size(price))` and is 0 at or above ref.
  - `est_share(side: SideScore, our_size: float) -> float`: `our/(score+our)`, or 0.0 when `not meets_target`.
  - `daily_pool_usd(program: dict) -> float`: `period_reward / 10_000 / days`, with days from `start_date` to `end_date` and a floor of 1/24.

- [ ] **Step 1: Write failing tests**
  - `test_book_from_fixture`: `Book.from_orderbook(fixture)` gives `best_yes_bid == 0.95`, `best_no_bid == 0.01`, `yes_ask == 0.99`, `mid() == 0.97` and `spread() == 0.04` (approx, 1e-9).
  - `test_book_handles_null_and_empty_sides`: `{"yes_dollars": None, "no_dollars": []}` gives empty sides and `mid() is None`.
  - `test_digest_changes_with_size`: two raws that differ in one size have different digests, and identical raws have equal ones.
  - `test_side_score_reference_and_discount`: levels `[(0.50,100),(0.49,150),(0.48,300),(0.47,1000)]`, target 1000, discount 0.5, and `price_ranges=[{"start":"0.0000","end":"1.0000","step":"0.0100"}]` give `ref_price == 0.49`, `qualifying_score == 650.0`, `depth == 1550` and `meets_target`.
  - `test_est_share_of_ten`: the side above with `our_size=10` gives `10/660` (approx).
  - `test_thin_side_scores_nothing`: levels `[(0.30,400),(0.29,500)]` with target 1000 give `meets_target is False` and `est_share == 0.0`.
  - `test_tick_size_subpenny`: ranges `[{"start":"0.0000","end":"0.1000","step":"0.0010"},{"start":"0.1000","end":"0.9000","step":"0.0100"},{"start":"0.9000","end":"1.0000","step":"0.0010"}]` give `tick_size(0.05) == 0.001`, `tick_size(0.5) == 0.01` and `tick_size(1.0) == 0.001`.
  - `test_daily_pool_usd`: `period_reward=1_000_000` from 2026-10-30T00:00Z to 2026-11-06T00:00Z gives `100/7` (approx).
- [ ] **Step 2: Run them.** Run: `.venv/bin/pytest -q tests/test_book.py tests/test_rewards.py`. Expected: FAIL.
- [ ] **Step 3: Implement `kxmm/book.py` and `kxmm/rewards.py`** per the Interfaces.
- [ ] **Step 4: Run them.** Expected: 8 passed.
- [ ] **Step 5: Commit** with message `feat: order-book parsing and Kalshi reward-share math`.

### Task 4: SQLite ledger

**Files:**
- Create: `kxmm/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: `book_digest` (Task 3).
- Produces: `connect(path: str | Path) -> sqlite3.Connection`. It creates the parent directory, sets WAL and `row_factory = sqlite3.Row`, and creates this schema:
  - `programs(id TEXT PRIMARY KEY, market_ticker TEXT, start_ts REAL, end_ts REAL, period_reward INTEGER, target_size REAL, discount_bps INTEGER, first_seen REAL, last_seen REAL, raw TEXT)`
  - `markets(ticker TEXT PRIMARY KEY, event_ticker TEXT, series_ticker TEXT, category TEXT, fee_type TEXT, close_ts REAL, status TEXT, price_ranges TEXT, updated_ts REAL, raw TEXT)`
  - `selections(id INTEGER PRIMARY KEY, ts REAL, tickers TEXT, detail TEXT)`
  - `books(id INTEGER PRIMARY KEY, ticker TEXT, ts REAL, digest TEXT, raw TEXT)`, indexed on `(ticker, ts)`
  - `trades(trade_id TEXT PRIMARY KEY, ticker TEXT, ts REAL, yes_price REAL, count REAL, taker_side TEXT)`, indexed on `(ticker, ts)`
  - `poll_state(ticker TEXT PRIMARY KEY, first_ok REAL, last_ok REAL)`
  - `gaps(id INTEGER PRIMARY KEY, ticker TEXT, start_ts REAL, end_ts REAL)`, where ticker `'*'` means the whole loop
  - `kv(k TEXT PRIMARY KEY, v TEXT)`
- Produces these functions:
  - `upsert_programs(con, programs: list[dict], now: float) -> None`
  - `upsert_markets(con, markets: list[dict], series_by_ticker: dict[str, dict], now: float) -> None`: series ticker = `event_ticker.split("-")[0]`.
  - `log_selection(con, ts: float, tickers: list[str], detail: list[dict]) -> None`
  - `store_book(con, ticker: str, ts: float, raw: dict) -> bool`: inserts only when the digest differs from that ticker's last stored book, and returns whether it inserted.
  - `store_trades(con, rows: list[dict]) -> int`: `INSERT OR IGNORE`, parsing `created_time` to epoch, `yes_price_dollars` and `count_fp` to float; returns the number of new rows.
  - `mark_poll(con, ticker: str, ts: float, gap_threshold_s: float) -> None`: when `ts - last_ok > threshold`, inserts `gaps(ticker, last_ok, ts)`; then sets `last_ok = ts` (and `first_ok` if new).
  - `heartbeat(con, ts: float, gap_threshold_s: float) -> None`: the same gap rule under ticker `'*'`, stored in `kv['heartbeat']`.
  - `last_heartbeat(con) -> float | None`
  - `record_restart_gaps(con, now: float, gap_threshold_s: float) -> int`: for `'*'` and every `poll_state` ticker whose `last_ok` is older than the threshold, inserts a gap ending at `now` and sets `last_ok = now`; returns the number of gaps.
  - `kv_get(con, k: str) -> str | None` and `kv_set(con, k: str, v: str) -> None`

- [ ] **Step 1: Write failing tests**, each on a `tmp_path` DB:
  - `test_store_book_dedupes_unchanged`: storing the same raw twice returns `True` then `False`, and there is 1 row.
  - `test_store_trades_dedupes_by_trade_id`: storing the fixture's trades twice returns `len(rows)` then `0`.
  - `test_mark_poll_gap_only_over_threshold`: polls at t=0, 200 and 600 with threshold 300 give exactly one gap `(200, 600)`.
  - `test_restart_gaps_cover_downtime`: `mark_poll` at t=0 and `heartbeat` at t=0, then `record_restart_gaps(now=1000, 300)`, returns 2, and the gaps are `('*', 0, 1000)` and `(ticker, 0, 1000)`.
  - `test_upsert_markets_derives_series`: the market fixture with series `{"KXSTREAMARRAY": series_fixture}` gives a row with `series_ticker == "KXSTREAMARRAY"`, `category == "Financials"` and `fee_type == "quadratic"`.
- [ ] **Step 2: Run them.** Run: `.venv/bin/pytest -q tests/test_db.py`. Expected: FAIL.
- [ ] **Step 3: Implement `kxmm/db.py`** per the Interfaces.
- [ ] **Step 4: Run them.** Expected: 5 passed.
- [ ] **Step 5: Commit** with message `feat: SQLite ledger with change-only books and gap tracking`.

### Task 5: Universe selector (pure)

**Files:**
- Create: `kxmm/select.py`
- Test: `tests/test_select.py`

**Interfaces:**
- Consumes: `Book`, `side_score`, `est_share` and `daily_pool_usd` (Task 3).
- Produces: `Candidate` dataclass with `ticker`, `event_ticker`, `series_ticker`, `daily_pool_usd`, `target_size`, `discount`, `mid`, `close_ts`, `maker_fee: bool` and `price_ranges: list[dict]`.
- Produces: `prefilter(programs: list[dict], markets: dict[str, dict], series: dict[str, dict], now: float, cfg: dict) -> list[Candidate]`. It applies the Global Constraints selection rules that need no book. Mid comes from the market's `yes_bid_dollars` and `yes_ask_dollars`, and `discount = discount_bps / 10_000`. When several programs cover one ticker, it uses the largest daily pool.
- Produces: `shortlist(cands: list[Candidate], n: int) -> list[Candidate]`: top `n` by `daily_pool_usd`.
- Produces: `Scored` dataclass with `cand`, `share_yes`, `share_no`, `est_daily_usd`, `collateral_usd` and `yield_`.
- Produces: `score(cand: Candidate, book: Book, cfg: dict) -> Scored | None`. It returns `None` when either side is `not meets_target`, or when a maker-fee series fails the fee rule. `est_daily_usd = daily_pool × (share_yes + share_no) / 2`, and `collateral_usd = quote_size × (ref_yes + ref_no)`.
- Produces: `choose(scored: list[Scored], current: list[str], max_markets: int, max_per_event: int) -> list[str]`. It keeps every `current` ticker still present in `scored` first, then adds by descending `yield_`, never exceeding `max_per_event` per `event_ticker` or `max_markets` in total.

- [ ] **Step 1: Write failing tests** using a helper `mk(ticker, event, mid, close_days, program_hours_left, pool_usd, fee_type="quadratic", mve=None)` that builds a program, market and series triple from `now = 1_800_000_000`:
  - `test_prefilter_excludes_by_rule`: mid 0.95, close in 2 days, 10 h of program left, `market_type="scalar"`, and an `mve_collection_ticker` are each excluded; a mid-0.50, 10-day, 72 h market is kept.
  - `test_prefilter_takes_largest_program_per_ticker`: two programs on one ticker ($14/day and $50/day) give one candidate with `daily_pool_usd == 50`.
  - `test_score_rejects_thin_side`: a book with a NO side below target returns `None`.
  - `test_score_values`: yes levels as in Task 3's reference example; NO side `[(0.49,100),(0.48,150),(0.47,300),(0.46,1000)]` (YES ask 0.51, uncrossed); target 1000; pool $100/day; quote 10. Gives `share_yes == share_no == 10/660`, `est_daily_usd == 100 × 10/660` and `collateral_usd == 10 × (0.49 + 0.48)`.
  - `test_score_maker_fee_rule`: with mid 0.5, a maker-fee series and an estimated reward per contract per day below `0.0175 × 0.25` returns `None`; raising the pool so it clears the fee returns a `Scored`.
  - `test_choose_is_sticky_and_caps_events`: a current ticker with the lowest yield is kept, no event gets more than 2, and the total equals `max_markets` when enough candidates exist.
- [ ] **Step 2: Run them.** Run: `.venv/bin/pytest -q tests/test_select.py`. Expected: FAIL.
- [ ] **Step 3: Implement `kxmm/select.py`** per the Interfaces.
- [ ] **Step 4: Run them.** Expected: 6 passed.
- [ ] **Step 5: Commit** with message `feat: reward-yield market selector with sticky universe`.

### Task 6: Recorder loop

**Files:**
- Create: `kxmm/recorder.py`, `kxmm/loop.py` (entry point `python -m kxmm.loop [--config config.yaml]`)
- Test: `tests/test_recorder.py`

**Interfaces:**
- Consumes: `KalshiPublic` and `NotFound` (Task 2), `Book` (Task 3), `db.*` (Task 4), `prefilter`, `shortlist`, `score` and `choose` (Task 5), and `keep_awake` (Task 1).
- Produces: `Recorder(cfg: dict, client, con, root: Path, clock=time.time, sleep=time.sleep)`, with `self.universe: list[str]` restored from the last `selections` row on construction. Methods:
  - `refresh_universe(now: float) -> list[str]`: programs → `markets_by_tickers` → series (cached in memory for 24 h) → `prefilter` → `shortlist(cfg shortlist_n)` → `orderbook` per shortlisted ticker (any exception skips that ticker) → `score` → `choose(current=self.universe)`. Then `upsert_programs`, `upsert_markets` and `log_selection`. Sets and returns `self.universe`.
  - `poll_books(now: float) -> None`: for each ticker, `orderbook` → `store_book` → `mark_poll`. `NotFound` removes the ticker from `self.universe` (logged INFO). Any other exception logs a WARNING and skips that ticker without `mark_poll`.
  - `poll_trades(now: float) -> None`: per ticker, `min_ts = int(kv 'trades_ts:<ticker>' or now - trades_backfill_s) - 1`, then `store_trades`, then saves the newest trade's ts. Exceptions are skipped as in `poll_books`.
  - `run() -> None`: `with keep_awake()`. Calls `record_restart_gaps`, then loops: if `root/out/KILL` exists, delete it, log "stopped by KILL" and return. Runs each of refresh, books and trades when its interval is due (refresh is also due at start), then `heartbeat`. Sleeps in 1-second slices up to the next due time, checking KILL between slices. Logs an INFO summary every 10 minutes (universe size, books and trades stored, gaps) and per-request detail at DEBUG.
- `kxmm/loop.py` configures logging (INFO, timestamps, to stdout), builds the client and connection from config, and calls `Recorder.run()`.

- [ ] **Step 1: Write failing tests** with a `FakeClient` that serves the fixtures and can be told to raise for a given ticker:
  - `test_refresh_universe_selects_and_logs`: programs naming two tickers that pass the filters (the fixture market edited to mid 0.50 and a 10-day close, and a book meeting the target) give `universe` of length 2 and one `selections` row.
  - `test_poll_books_stores_changes_only`: two polls with an identical book give 1 `books` row and an advanced `poll_state.last_ok`.
  - `test_poll_books_survives_one_failing_market`: ticker A raises `Unavailable` and B succeeds. B has a book row, A has no `poll_state` update, and no exception escapes.
  - `test_poll_books_drops_market_on_not_found`: after the poll, the ticker is no longer in `universe`.
  - `test_poll_trades_advances_cursor_ts`: the second poll's `min_ts` equals the newest stored trade ts − 1.
  - `test_run_exits_on_kill_file`: with a fake clock and sleep, creating `out/KILL` after the first cycle makes `run()` return within 2 sleep slices and delete the file; `kv['heartbeat']` is set.
- [ ] **Step 2: Run them.** Run: `.venv/bin/pytest -q tests/test_recorder.py`. Expected: FAIL.
- [ ] **Step 3: Implement `kxmm/recorder.py` and `kxmm/loop.py`** per the Interfaces.
- [ ] **Step 4: Run them.** Expected: 6 passed.
- [ ] **Step 5: Commit** with message `feat: recorder loop with KILL-file stop and heartbeat`.

### Task 7: Health report and cross-platform launcher

**Files:**
- Create: `kxmm/health.py`, `kxmm.py`
- Test: `tests/test_health.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `db.connect`, `last_heartbeat` and `kv_get` (Task 4), and `load_config` (Task 1).
- Produces: `report(con, now: float, window_s: float = 86400, gap_threshold_s: float = 300, min_markets: int = 20) -> dict` with these keys:
  - `universe`: the size of the last selection.
  - `books_window` and `trades_window`: rows inside the window.
  - `clean_markets`: tickers whose `poll_state.first_ok <= now - window_s`, `last_ok >= now - gap_threshold_s`, and that have no gap overlapping the window.
  - `loop_gaps`: `'*'` gaps overlapping the window.
  - `max_gap_s`
  - `phase1_pass = len(clean_markets) >= min_markets and not loop_gaps`
- Produces: `kxmm.py` with these commands:
  - `start`: refuses when the heartbeat is younger than `3 × book_poll_s`. Otherwise it removes a stale `out/KILL`, launches `[sys.executable, "-u", "-m", "kxmm.loop"]` with stdout and stderr to `output.log_path` (append, utf-8) and `**spawn_kwargs(sys.platform)`, and prints the pid.
  - `stop`: creates `out/KILL`, then waits up to 120 s for the loop to delete it. If the loop never does, it prints "loop did not confirm stop; run status" and removes the file.
  - `status`: prints RUNNING or STOPPED (heartbeat age), then the `report()` fields, then `PHASE 1: PASS/NOT YET`.
  - `log`: prints the last 40 lines of the log.
  - `test`: runs `[sys.executable, "-m", "pytest", "-q"]`.
  - `run`: the loop in the foreground.
- Produces: `spawn_kwargs(platform: str) -> dict`, which gives `{"creationflags": 0x00000008 | 0x00000200}` (DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP) on `"win32"` and `{"start_new_session": True}` otherwise.
- Produces: `loop_state(con, now: float, book_poll_s: float) -> tuple[str, float | None]`, returning `("RUNNING" | "STOPPED", age_s)`.

- [ ] **Step 1: Write failing tests**
  - `test_report_phase1_pass`: 20 tickers polled every 10 s from `now - 86400 - 60` to `now`, plus heartbeats, give `phase1_pass is True`.
  - `test_report_gap_breaks_pass`: the same with one 400 s gap on one ticker gives 19 `clean_markets` and `phase1_pass is False`.
  - `test_report_loop_gap_breaks_pass`: a `'*'` gap inside the window gives `phase1_pass is False`.
  - `test_status_stale_heartbeat_is_stopped`: a heartbeat 100 s old with `book_poll_s=10` gives `"STOPPED"`, and one 5 s old gives `"RUNNING"`.
  - `test_spawn_kwargs_per_platform`.
  - `test_stop_creates_kill_and_times_out_cleanly`: with a 0.2 s wait override, `stop` creates `out/KILL`, prints the "did not confirm" line, and leaves no `KILL` behind.
- [ ] **Step 2: Run them.** Run: `.venv/bin/pytest -q tests/test_health.py tests/test_cli.py`. Expected: FAIL.
- [ ] **Step 3: Implement `kxmm/health.py` and `kxmm.py`** per the Interfaces. Put the `stop` wait timeout behind a module constant so the test can override it.
- [ ] **Step 4: Run them.** Expected: 6 passed.
- [ ] **Step 5: Commit** with message `feat: health report and cross-platform launcher`.

### Task 8: Windows wrappers, README, live smoke run

**Files:**
- Create: `kxmm.ps1`, `kxmm.sh`, `README.md`

**Interfaces:**
- Consumes: `kxmm.py` (Task 7).
- `kxmm.ps1` runs `& "$PSScriptRoot\.venv\Scripts\python.exe" "$PSScriptRoot\kxmm.py" @args`. `kxmm.sh` runs the same with `.venv/bin/python` and is executable.

- [ ] **Step 1: Write `README.md`.** It covers what Phase 1 does, the commands, and the Phase 1 pass rule. Windows setup is adapted from `~/Desktop/Project/lpfarm/README-WINDOWS.md`:
  - Install Python 3.12.
  - Copy the folder without `.venv`, `out` and `.env`.
  - Run `python -m venv .venv` and `pip install -r requirements.txt`.
  - Run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.
  - Run `powercfg /change standby-timeout-ac 0` and `powercfg /change hibernate-timeout-ac 0`.
  - Never use Task Manager End task; use `.\kxmm.ps1 stop`.
  - Run on one machine only.
- [ ] **Step 2: Run the full suite.** Run: `.venv/bin/pytest -q`. Expected: all tests pass (45).
- [ ] **Step 3: Run a live foreground smoke test** for 5 minutes: `timeout 300 .venv/bin/python kxmm.py run` (gtimeout on macOS if `timeout` is missing), then `.venv/bin/python kxmm.py status`. Expected:
  - The universe has ≥ 20 markets.
  - `books_window > 0` and `trades_window >= 0`.
  - The log has no tracebacks.
  - The first refresh finishes in under 300 s (the log shows its duration).
- [ ] **Step 4: Run a launcher round trip.** Run `./kxmm.sh start`, then `./kxmm.sh status` after 30 s (RUNNING), then `./kxmm.sh stop` (it confirms within 120 s), then `./kxmm.sh status` (STOPPED). Expected: as stated, with `out/KILL` absent afterwards.
- [ ] **Step 5: Commit** with message `feat: Windows/macOS wrappers and README; Phase 1 recorder ready`.

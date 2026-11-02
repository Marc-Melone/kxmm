# kxmm Phase 2 (Simulator and Gate A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replay the recorder's books and trades through a pre-registered quoting strategy and a strict fill model, then print the Gate A verdict and the live sample-size target.

**Architecture:**
- Pure modules for fair value and volatility, quoting, fills, reward share and statistics.
- A per-market event replay that reads the recorder DB **read-only**, so it can run on Windows while the recorder writes.
- Results go to a separate `out/sim.db` plus a CSV, and `python kxmm.py sim` prints the report.

**Tech Stack:** Python 3.12+, stdlib `sqlite3`/`random`/`statistics`/`bisect`, existing `requests` client for settlement lookups. No new dependencies.

**Spec:** `DESIGN.md` ("The test", "How it's built" items 4–6, "Phases, gates and timeline", "Reward and rate facts"). Pre-registration lives in `STRATEGY.md` (written in Task 1).

**Scope:** Gate A only.
- The order client, demo exchange and live ledger come in Phase 3 and 4 plans.
- **The dashboard (:5120) is deferred:** the report is text plus a CSV, and the user reads results through Claude and the plan doc. *(My call.)*

## Global Constraints

- **The test** (DESIGN "The test"):
  - Primary per market-day = Σ fills (10-min markout P&L − fees) + liquidity rewards × haircut. Interest is excluded.
  - Trading-only (rewards excluded) is reported beside it.
  - "Beyond chance" means bootstrapping calendar days (UTC): the 2.5th percentile of the mean daily total must be > 0.
- **Gate A** (DESIGN): simulated net > 0 in **≥ 3 markets** over **≥ 10 recorded days**, under the strict queue rule. The report prints the live sample-size target. On the Gate A date, Claude records it in STRATEGY.md's "Gate A result" section (a section Task 1 creates empty) before any live order.
- **Strict queue rule:**
  - A trade through our price fills us up to its size.
  - A trade at our price fills us only beyond the displayed queue ahead of us when we joined. That queue only shrinks: by at-price trades, and down to the displayed size when the book shows less.
  - Requoting to a new price puts us at the back.
- **Fees:** maker fee `ceil_to_cent(0.0175 × C × P × (1−P))` on fills in series with `fee_type == "quadratic_with_maker_fees"`; 0 otherwise. The strategy never takes liquidity.
- **Kalshi position netting:** position is signed YES contracts. A NO-bid fill at price q equals selling YES at 1−q.
- **Blackout:** no quoting within `blackout_before_s` = 259,200 s (3 days) of `close_time` or `occurrence_datetime`, whichever is earlier. No quoting inside recorded gaps; resting orders are cancelled at a gap's start.
- **Read-only access:** the recorder DB is opened read-only through a `file:` URI (`Path.as_uri()` + `?mode=ro`). The sim writes only `out/sim.db` and `out/sim_market_days.csv`.
- **Determinism:** the same DB and config give the same output. The bootstrap seed is fixed in config.
- **Pinned deps unchanged:** `requests==2.34.2`, `PyYAML==6.0.3`, `pytest==9.1.1`.
- **Commits** end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Recording gaps mid-replay** (the Windows box slept or rebooted). Expected: resting orders are cancelled at the gap's start, with no fills or rewards inside it, and a new queue position after it. Pinned in Task 7 (`test_gap_cancels_orders_and_stops_rewards`).
2. **Markets that settle during or after the replay, and ones still open.** Expected: settlement markouts only where the result is known (`result` yes/no). Open markets are marked at their last mid. A missing result never crashes the run. Pinned in Task 9 (`test_settlement_known_and_unknown`).
3. **Crossed or one-sided recorded books.** Expected: no quotes, no crash. Pinned in Task 4 (`test_no_quotes_on_crossed_or_one_sided_book`).
4. **Fractional trade counts and both taker sides** (`count_fp "15.37"`, `taker_side "no"`). Expected: correct side mapping and fractional fills. Pinned in Task 5 (`test_fractional_no_taker_fills_yes_bid`).
5. **The recorder writing while the sim reads.** Expected: the sim opens read-only and never writes or locks the recorder DB. Pinned in Task 2 (`test_open_readonly_refuses_writes`).

---

### Task 1: Pre-registration and sim config

**Files:**
- Create: `STRATEGY.md`
- Modify: `config.yaml` (add the `sim:` section)
- Test: `tests/test_strategy.py`

**Interfaces:**
- Produces: `cfg["sim"]` keys exactly as below. All later tasks read their parameters from here.

- [ ] **Step 1: Write failing tests**
  - `test_sim_config_values`: `load_config()["sim"]` has `strategy_version == "1.0.0"`, `quote_size == 10`, `stake_usd == 300`, `max_loss_per_market_frac == 0.05`, `gamma == 0.1`, `vol_halflife_s == 3600`, `horizon_s == 86400`, `blackout_before_s == 259200`, `reward_haircut == 0.5`, `markouts_s == [60, 600, 3600]`, `primary_markout_s == 600`, `bootstrap_n == 10000`, `bootstrap_seed == 7`, `gate_a_min_markets == 3`, `gate_a_min_days == 10`, `live_markets == 5`.
  - `test_strategy_md_version_matches_config`: the first line of STRATEGY.md matching `Strategy version: (\S+)` equals `cfg["sim"]["strategy_version"]`. This is the void-policy guard.
- [ ] **Step 2: Run them.** Run: `.venv/bin/pytest -q tests/test_strategy.py`. Expected: FAIL.
- [ ] **Step 3: Add the `sim:` section to `config.yaml`** with exactly the values above, each with an inline comment. **Write `STRATEGY.md`**, starting with the line `Strategy version: 1.0.0 (pre-registered 2026-11-02, before any recorded day was simulated)`. Its sections:
  - **Thesis.** One paragraph: lpfarm's paper edge was never checked against real fills; this sim is the Kalshi paper step, and live fills decide.
  - **Quoting.**
    - Fair value is the microprice in log-odds.
    - EWMA variance of log-odds changes, half-life 3,600 s.
    - Reservation = fair − (position/quote_size) × γ × σ² × τ, with τ = min(horizon, time to blackout).
    - Risk half-width = ½ × γ × σ² × τ.
    - Each side quotes at the reward reference price or further from mid, whichever the reservation ± half-width gives. Bids are rounded down to the market's tick and never cross the opposite best.
    - Size is `quote_size`, cut so that worst-case loss stays ≤ 5% of stake ($15). At the cap, the side that would add is pulled.
    - Requote only when a target price moves ≥ 1 tick or the size changes.
    - A–S's fill-intensity term `k` is **not** used in v1.0.0; it is fitted from simulated fills at Gate A.
  - **Fill model:** the strict queue rule, verbatim from Global Constraints.
  - **Rewards:**
    - Kalshi's scoring as in DESIGN, with our order included in the book.
    - Accrued over time between book changes, only while both sides meet the target.
    - Daily pool = `period_reward` ÷ 10,000 ÷ program days.
    - Reported raw and × 0.5 haircut; the **primary metric uses the haircut**.
  - **Metrics and the test:** as in Global Constraints, with markouts at 60 s, 600 s (primary) and 3,600 s, plus settlement.
  - **Gate A:** as in Global Constraints. It reports the per-fill mean and s.d. of the 600 s markout return, `fills_needed = ceil((2·sd/mean)²)`, and the implied live days at 5 markets.
  - **Void policy:** changing any parameter above bumps the version and starts a new record segment; tuning uses the first 5 recorded days only.
  - **Gate A result:** empty until Gate A.
  - **Changelog:** empty.
- [ ] **Step 4: Run them.** Expected: 2 passed.
- [ ] **Step 5: Commit** with message `docs: pre-register strategy v1.0.0 and sim config`.

### Task 2: Read-only replay source

**Files:**
- Create: `kxmm/replay.py`
- Test: `tests/test_replay.py`

**Interfaces:**
- Consumes: `db.connect` / `store_book` / `store_trades` / `mark_poll` / `add_gap` / `upsert_markets` / `upsert_programs` (Phase 1, used by tests to build fixtures), and `Book`.
- Produces:
  - `open_readonly(path: str | Path) -> sqlite3.Connection`, opened with `sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)` and `row_factory = sqlite3.Row`.
  - `Meta` dataclass: `ticker`, `price_ranges: list[dict]`, `close_ts: float`, `occurrence_ts: float | None`, `maker_fee: bool`.
  - `market_meta(con, ticker) -> Meta`, from `markets.raw`.
  - `Program` dataclass: `start: float`, `end: float`, `daily_pool: float`, `target: float`, `discount: float`.
  - `programs_for(con, ticker) -> list[Program]`, from `programs` rows.
  - `program_at(programs: list[Program], ts: float) -> Program | None`: the largest daily pool among programs active at `ts`.
  - `Trade` dataclass: `ts`, `yes_price`, `count`, `taker_side`.
  - `Event` dataclass: `ts: float`, `kind: str` (`"book"`, `"trade"`, `"gap_start"` or `"gap_end"`), `book: Book | None`, `trade: Trade | None`.
  - `events(con, ticker, t0: float, t1: float) -> list[Event]`: in time order. At equal ts the order is `gap_end`, `book`, `trade`, `gap_start`.
  - `MidSeries(con, ticker)` with `.at(ts) -> float | None`: the mid of the last book at or before `ts`. It is `None` before the first book, inside a gap, or when that book has no mid.
  - `tickers_in(con, t0, t1) -> list[str]`: tickers with any book in range.
  - `recorded_days(con) -> int`: distinct UTC days with any book.

- [ ] **Step 1: Write failing tests.** A helper `make_db(tmp_path)` builds a recorder DB with one market `K`:
  - Books at t = 0 (yes 0.48/no 0.50), t = 100 (yes 0.49/no 0.50) and t = 400.
  - Trades at t = 50 and t = 150.
  - A gap from 200 to 300.

  The tests:
  - `test_events_in_time_order_with_gaps`: kinds `[book, trade, book, trade, gap_start, gap_end, book]`.
  - `test_mid_series_respects_gaps`: `at(120) == 0.495` (yes bid 0.49, yes ask 0.50), `at(250) is None`, `at(-1) is None`.
  - `test_program_at_picks_largest_active`.
  - `test_market_meta_reads_occurrence_and_fee`: uses the fixture `market.json` with series fee type `quadratic_with_maker_fees`, so `maker_fee is True`; `occurrence_ts` is parsed.
  - `test_open_readonly_refuses_writes`: an INSERT raises `sqlite3.OperationalError`.
- [ ] **Step 2: Run them.** Run: `.venv/bin/pytest -q tests/test_replay.py`. Expected: FAIL.
- [ ] **Step 3: Implement `kxmm/replay.py`** per the Interfaces. `MidSeries` holds sorted book times and mids plus the gap intervals, and uses `bisect`.
- [ ] **Step 4: Run them.** Expected: 5 passed.
- [ ] **Step 5: Commit** with message `feat: read-only replay source over the recorder DB`.

### Task 3: Fair value and volatility

**Files:**
- Create: `kxmm/fair.py`
- Test: `tests/test_fair.py`

**Interfaces:**
- Consumes: `Book`.
- Produces:
  - `microprice(book: Book) -> float | None`: `(bid × ask_size + ask × bid_size) / (bid_size + ask_size)`. Bid is the best YES bid; ask = 1 − best NO bid, with ask_size = the size of the best NO bid.
  - `logit(p: float) -> float` and `expit(x: float) -> float`. `logit` clamps p to [0.001, 0.999].
  - `class EwmaVol(halflife_s: float)` with `update(ts: float, x: float) -> None` and the property `var_per_s -> float | None`, which is `None` until two updates. Each update adds `(Δx)² / Δt` with weight `1 − 0.5 ** (Δt / halflife_s)`. An update with Δt ≤ 0 is ignored.

- [ ] **Step 1: Write failing tests**
  - `test_microprice_leans_to_thin_side`: YES bid 0.49 × 100 and NO bid 0.49 × 300 (ask 0.51) give `0.49×300/400 + 0.51×100/400 = 0.495`.
  - `test_microprice_none_on_one_side`.
  - `test_logit_expit_roundtrip`: also checks `logit(0.0)` is finite.
  - `test_ewma_converges_to_constant_rate`: x alternates ±0.1 every 60 s for 500 steps, so `var_per_s ≈ 0.04/60` within 1%. (Each step moves 0.2; 0.2² / 60.)
- [ ] **Step 2: Run them.** Run: `.venv/bin/pytest -q tests/test_fair.py`. Expected: FAIL.
- [ ] **Step 3: Implement `kxmm/fair.py`** per the Interfaces.
- [ ] **Step 4: Run them.** Expected: 4 passed.
- [ ] **Step 5: Commit** with message `feat: microprice, log-odds and EWMA volatility`.

### Task 4: Quoting engine (pure)

**Files:**
- Create: `kxmm/quoting.py`
- Test: `tests/test_quoting.py`

**Interfaces:**
- Consumes: `Book`, `side_score` and `tick_size` (rewards), `microprice` / `logit` / `expit` (Task 3), `Meta` and `Program` (Task 2).
- Produces:
  - `Quotes` dataclass: `yes_bid: float | None`, `yes_size: float`, `no_bid: float | None`, `no_size: float`.
  - `make_quotes(book: Book, position: float, var_per_s: float | None, now: float, meta: Meta, program: Program | None, scfg: dict) -> Quotes | None`. `scfg = cfg["sim"]`. Rules, in order:
    1. Return `None` when any of these holds: no program, a missing side, a crossed book (`best_yes_bid >= yes_ask`), `var_per_s is None`, or `now >= min(close_ts, occurrence_ts or inf) − blackout_before_s`.
    2. Compute x̂ = logit(microprice), τ = `min(horizon_s, blackout_time − now)` and `q = position / quote_size`.
    3. Reservation x_r = x̂ − q·γ·var·τ, with half-width δ = ½·γ·var·τ.
    4. YES side: target = expit(x_r − δ), floored to the tick. Then `yes_bid = min(ref_yes, target)`, where `ref_yes` comes from `side_score(book.yes, …)`; if `ref_yes` is None, use the target. Then `yes_bid = min(yes_bid, yes_ask − tick)`.
    5. NO side: target = 1 − expit(x_r + δ), floored to the tick, then `no_bid = min(ref_no, target, (1 − best_yes_bid) − tick)`.
    6. Sizes: `cap = max_loss_per_market_frac × stake_usd`. If `position ≥ 0`: `yes_size = min(quote_size, max(0, cap/mid − position))` and `no_size = quote_size`. Mirror it for `position < 0` using `cap/(1 − mid)` on the NO side. A size of 0 gives a price of `None`.

- [ ] **Step 1: Write failing tests.** The book is helpers `YES`/`NO` (YES ask 0.51, mid 0.505), with a program of target 1000, discount 0.5 and pool $100.
  - `test_flat_position_quotes_at_reference_when_risk_is_small`: `var_per_s = 1e-9`, giving `yes_bid == 0.49` and `no_bid == 0.48`, both sizes 10.
  - `test_long_position_skews_down_and_caps`: with `var_per_s = 1e-5` and `position = 20`, the shift is 2 lots × 0.1 × 1e-5 × 86,400 ≈ 0.17 log-odds. The YES target falls to about 0.45, so `yes_bid < 0.49`. At `position = 30` (cap/mid = 15/0.505 ≈ 29.7), `yes_bid is None`, while `no_bid` is not None.
  - `test_blackout_returns_none`: `now = close_ts − 259200 + 1` returns None.
  - `test_no_quotes_on_crossed_or_one_sided_book`: returns None.
  - `test_subpenny_ticks_round_down`: with ranges having step 0.001 below 0.10 and a book near 0.05, `round(yes_bid * 1000)` is an integer.
- [ ] **Step 2: Run them.** Run: `.venv/bin/pytest -q tests/test_quoting.py`. Expected: FAIL.
- [ ] **Step 3: Implement `kxmm/quoting.py`** per the Interfaces.
- [ ] **Step 4: Run them.** Expected: 5 passed.
- [ ] **Step 5: Commit** with message `feat: pre-registered quoting engine v1.0.0`.

### Task 5: Strict-queue fill model (pure)

**Files:**
- Create: `kxmm/fills.py`
- Test: `tests/test_fills.py`

**Interfaces:**
- Consumes: `Book` and `Trade` (Task 2).
- Produces:
  - `Resting` dataclass: `side: str` (`"yes"` or `"no"`, meaning a YES bid or a NO bid), `price: float`, `size: float`, `queue_ahead: float`.
  - `place(side: str, price: float, size: float, book: Book) -> Resting`: `queue_ahead` = the displayed size at exactly that price on that side, or 0.
  - `on_book(r: Resting, book: Book) -> None`: `queue_ahead = min(queue_ahead, displayed size at r.price)`.
  - `on_trade(r: Resting, t: Trade) -> float`: returns the contracts filled and decrements `r.size`. Mapping: `taker_side == "no"` hits YES bids at `t.yes_price`; `taker_side == "yes"` hits NO bids at `1 − t.yes_price`. A level better than r's price fills nothing. A price worse than r's (through) fills `min(r.size, t.count)`. At r's price (|diff| < 1e-9), the count first consumes `queue_ahead`, and only the remainder fills.

- [ ] **Step 1: Write failing tests**
  - `test_through_trade_fills_up_to_count`: a YES bid at 0.49 × 10 and a trade with `taker_side "no"` at yes_price 0.47 × 4 fill 4, leaving size 6.
  - `test_at_price_waits_for_queue`: a YES bid at 0.49 placed behind 150. A 100-contract trade at 0.49 fills 0 (queue drops to 50). Then a 60-contract trade fills 10.
  - `test_queue_shrinks_with_displayed_size`: queue 150, then a book shows 40 at 0.49, so queue becomes 40.
  - `test_better_level_trade_does_not_fill`: a trade at 0.50 against our 0.49 bid fills 0.
  - `test_fractional_no_taker_fills_yes_bid`: count 15.37 through our 0.49 YES bid of size 20 fills 15.37.
  - `test_yes_taker_hits_no_bid`: a NO bid at 0.48 (YES-equivalent 0.52) and a trade with `taker_side "yes"` at yes_price 0.53 (NO level 0.47, through) fill.
- [ ] **Step 2: Run them.** Run: `.venv/bin/pytest -q tests/test_fills.py`. Expected: FAIL.
- [ ] **Step 3: Implement `kxmm/fills.py`** per the Interfaces.
- [ ] **Step 4: Run them.** Expected: 6 passed.
- [ ] **Step 5: Commit** with message `feat: strict-queue fill model`.

### Task 6: Reward share with our orders in the book

**Files:**
- Modify: `kxmm/rewards.py`
- Test: `tests/test_rewards.py` (append)

**Interfaces:**
- Consumes: `side_score`, `tick_size`.
- Produces: `our_side_share(levels: list[tuple[float, float]], our_price: float | None, our_size: float, target_size: float, discount: float, price_ranges: list[dict]) -> float`.
  - It adds our size at our price (merging into an existing level), recomputes the reference and the qualifying levels, and returns `our_score / qualifying_score`.
  - It returns 0 when our price is None, the side misses the target, or our level falls outside the qualifying depth.

- [ ] **Step 1: Write failing tests**
  - `test_our_share_at_reference_matches_estimate`: Task 3 of Phase 1's levels (`[(0.50,100),(0.49,150),(0.48,300),(0.47,1000)]`) with ours at 0.49 × 10 give `10/660`.
  - `test_our_share_behind_target_depth_is_zero`: ours at 0.40 gives 0.
  - `test_our_share_discounted_one_tick_back`: ours at 0.48 × 10 scores 5 out of 655 (100 + 150 + (300 + 10) × 0.5 + 1000 × 0.25), so the result is `5/655`.
- [ ] **Step 2: Run them.** Run: `.venv/bin/pytest -q tests/test_rewards.py`. Expected: FAIL (3 new).
- [ ] **Step 3: Implement `our_side_share`** in `kxmm/rewards.py`.
- [ ] **Step 4: Run them.** Expected: 8 passed (5 old + 3 new).
- [ ] **Step 5: Commit** with message `feat: reward share with our resting orders included`.

### Task 7: Per-market simulator

**Files:**
- Create: `kxmm/sim.py`
- Test: `tests/test_sim.py`

**Interfaces:**
- Consumes: Task 2 (`events`, `MidSeries`, `market_meta`, `programs_for`, `program_at`), Task 3 (`EwmaVol`, `microprice`, `logit`), Task 4 (`make_quotes`), Task 5 (`place`, `on_book`, `on_trade`), Task 6 (`our_side_share`).
- Produces:
  - `Fill` dataclass: `ticker`, `ts`, `side`, `price`, `size`, `fee`, `markouts: dict[int, float | None]` (YES-space P&L per horizon, fee excluded), `position_after`.
  - `MarketResult` dataclass: `ticker`, `fills: list[Fill]`, `reward_by_day: dict[str, float]` (raw dollars by UTC `YYYY-MM-DD`), `quoted_s_by_day: dict[str, float]`, `position: float`, `cash: float`, `last_mid: float | None`.
  - `maker_fee(contracts: float, price: float) -> float`: `ceil(0.0175·C·P·(1−P)·100)/100`.
  - `simulate_market(con, ticker: str, t0: float, t1: float, cfg: dict) -> MarketResult`. Loop over `events()`:
    - **book:** first accrue rewards for the elapsed interval, using the previous book and the current resting orders, if not in a gap and a program is active: `pool/86400 × dt × (share_yes + share_no)/2`. Then update vol with `logit(microprice)`, run `on_book` for resting orders, then requote.
    - **trade:** run `on_trade` for each resting order. A fill updates position and cash (YES bid: +c, cash −p·c; NO bid: −c, cash +(1−p)·c), subtracts the fee from cash, and records a Fill. Then requote.
    - **gap_start:** cancel all resting orders; mark the gap.
    - **gap_end:** clear the gap mark.
    - **Requote** = `make_quotes(...)`. Replace a side's resting order only when the new price differs by ≥ 1 tick or the new size differs; `None` cancels.
    - **After the loop:** fill markouts from `MidSeries.at(fill.ts + h)`. YES fill: `(mid − p)·c`; NO fill: `((1 − p) − mid)·c`.

- [ ] **Step 1: Write failing tests** on synthetic recorder DBs from a helper `build(tmp_path, books, trades, gaps, close_days=10)`. The market has program target 100, discount 0.5 and pool $86.40/day, so $1 per 1000 s at full share.
  - `test_through_trade_creates_fill_with_markout`: the mid moves from 0.495 to 0.505 by t + 600, so the YES fill's `markouts[600] == (0.505 − price) × size`.
  - `test_rewards_accrue_only_with_both_sides_meeting_target`: one hour of a book meeting the target on both sides gives raw reward > 0. The same hour with the NO side under target gives 0.
  - `test_gap_cancels_orders_and_stops_rewards`: a through trade inside a gap does not fill; reward over the gap is 0; after `gap_end` and a book, the order is placed again with the queue taken from that book.
  - `test_blackout_stops_quoting`: `close_days=2` gives no fills and no rewards.
  - `test_inventory_cap_pulls_adding_side`: repeated through trades on the YES side stop filling once position × mid ≥ $15.
- [ ] **Step 2: Run them.** Run: `.venv/bin/pytest -q tests/test_sim.py`. Expected: FAIL.
- [ ] **Step 3: Implement `kxmm/sim.py`** per the Interfaces.
- [ ] **Step 4: Run them.** Expected: 5 passed.
- [ ] **Step 5: Commit** with message `feat: per-market replay simulator`.

### Task 8: Statistics and Gate A

**Files:**
- Create: `kxmm/stats.py`
- Test: `tests/test_stats.py`

**Interfaces:**
- Consumes: `MarketResult` and `Fill` (Task 7).
- Produces:
  - `MarketDay` dataclass: `ticker`, `day`, `fills`, `trading`, `reward_raw`, `reward`, `total`. Here `trading` = Σ(markouts[primary] − fee) over fills with a known markout, `reward = reward_raw × haircut`, and `total = trading + reward`.
  - `market_days(results: list[MarketResult], scfg: dict) -> list[MarketDay]`: one row per (ticker, day) with quoting time > 0.
  - `day_totals(mds: list[MarketDay], field: str) -> dict[str, float]`.
  - `bootstrap_lower(values: list[float], n: int, seed: int, q: float = 0.025) -> float`: uses `random.Random(seed)`, resampling the mean.
  - `t_stat(values) -> float | None`.
  - `FillStats` dataclass: `n`, `mean`, `sd`, `fills_needed: int | None`, `per_market_day: float`. The return per fill = markouts[primary] / (price × size).
  - `fill_stats(results, mds, scfg) -> FillStats`.
  - `GateA` dataclass: `passed: bool`, `markets_positive: int`, `days: int`, `reasons: list[str]`, `live_days_needed: float | None` (= fills_needed / (per_market_day × live_markets)).
  - `gate_a(mds, recorded_days: int, fs: FillStats, scfg) -> GateA`.

- [ ] **Step 1: Write failing tests**
  - `test_market_days_attribution`: two fills on one day (markout 0.30 and −0.10, fees 0.01 each) plus raw reward 1.00 give `trading == 0.18`, `reward == 0.50` and `total == 0.68`.
  - `test_bootstrap_lower_is_deterministic_and_sensible`: values `[1, 2, 3, 4, 5] × 4` with seed 7 give a lower bound > 0, equal across two calls. Values of mean 0 give a lower bound ≤ 0.
  - `test_fill_stats_sample_size`: returns of mean 0.0364 and s.d. 0.1584 (built from a two-point set) give `fills_needed == 76`.
  - `test_gate_a_needs_days_and_markets`: 3 positive markets over 9 days gives `passed is False`, with reasons mentioning "9 of 10 days". Over 10 days it passes. With 2 positive markets it fails, with reasons mentioning "2 of 3 markets".
- [ ] **Step 2: Run them.** Run: `.venv/bin/pytest -q tests/test_stats.py`. Expected: FAIL.
- [ ] **Step 3: Implement `kxmm/stats.py`** per the Interfaces.
- [ ] **Step 4: Run them.** Expected: 4 passed.
- [ ] **Step 5: Commit** with message `feat: market-day attribution, day bootstrap and Gate A`.

### Task 9: Sim runner, settlement, CLI and report

**Files:**
- Create: `kxmm/simrun.py`
- Modify: `kxmm/cli.py` (add `sim`), `README.md` (a "Phase 2: simulate" section)
- Test: `tests/test_simrun.py`

**Interfaces:**
- Consumes: Tasks 2, 7 and 8; `KalshiPublic.market(ticker)`, a new client method that returns the `"market"` object.
- Produces:
  - `settlements(client, tickers, cache_con) -> dict[str, float | None]`: 1.0 / 0.0 for `result` yes/no, else None. Results are cached in `out/sim.db` table `settlements(ticker PRIMARY KEY, value, fetched_ts)`. A `client=None` means cache-only.
  - `run(cfg, db_path: Path, out_dir: Path, t0: float | None, t1: float | None, client=None) -> str`. It returns the report text and writes three things:
    - `out/sim.db` with tables `runs(id, ts, strategy_version, params, t0, t1)` and `market_days(run_id, ticker, day, fills, trading, reward_raw, reward, total)`.
    - `out/sim_market_days.csv`.
    - Per-fill settlement P&L, reported as "inventory luck" totals.
  - The report text, in this order:
    - strategy version, days, markets, fills
    - per-market totals, best first
    - day-level mean, t-stat and bootstrap lower bound, for both "total" and "trading only"
    - fill stats and `fills_needed`
    - settlement P&L (known / unknown counts)
    - `GATE A: PASS` or `GATE A: NOT YET — <reasons>`
    - `LIVE TARGET: <fills_needed> fills ≈ <live_days_needed> days at 5 markets`
  - CLI: `python kxmm.py sim [--db out/kxmm.db] [--from YYYY-MM-DD] [--to YYYY-MM-DD] [--offline]`. It uses the public client unless `--offline` is given.

- [ ] **Step 1: Write failing tests**
  - `test_run_end_to_end_report`: a synthetic recorder DB with 2 markets and 1 day, with a fake client returning `result` yes for one market and `""` for the other. The report contains `"Strategy version 1.0.0"`, `"GATE A: NOT YET"` and `"1 of 10 days"`. The CSV has a header plus at least 1 row. `out/sim.db` has 1 run.
  - `test_settlement_known_and_unknown`: the values map is `{A: 1.0, B: None}`, a second call with `client=None` reads the cache, and nothing raises.
  - `test_cli_sim_offline`: `cli.main(["sim", "--db", <db>, "--offline"], root=tmp)` returns 0 and prints `"GATE A"`.
- [ ] **Step 2: Run them.** Run: `.venv/bin/pytest -q tests/test_simrun.py`. Expected: FAIL.
- [ ] **Step 3: Implement** `kxmm/simrun.py`, the `KalshiPublic.market()` method, the `sim` command in `kxmm/cli.py`, and the README section (commands; how to copy `out\kxmm.db` from Windows or run `.\kxmm.ps1 sim` there; how to read the report).
- [ ] **Step 4: Run them.** Expected: 3 passed. Then run the full suite: `.venv/bin/pytest -q`. Expected: all pass.
- [ ] **Step 5: Smoke-run on the Mac's recorded data:** `.venv/bin/python kxmm.py sim --offline`. Expected:
  - a report prints with no traceback
  - `GATE A: NOT YET` (under 1 recorded day)
  - the recorder DB's mtime is unchanged (read-only)
- [ ] **Step 6: Commit** with message `feat: sim runner, settlement cache and Gate A report`.

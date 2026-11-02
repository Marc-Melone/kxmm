# kxmm Phase 3 (Demo Trader and Gate B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A trading loop that runs the Phase 2 quoting engine against Kalshi's **demo** exchange with real order plumbing, safety stops and scripted drills, and prints the Gate B verdict.

**Architecture:**
- A signed REST client that can only reach demo URLs. Pure modules for order diffing, P&L and risk. A trader loop separate from the recorder: it has its own `out/trade/` folder, KILL/HALT files and database.
- Drills exercise every safety path against demo, and `trade status` judges Gate B.
- Fills and positions are **polled over REST** (every cycle, and reconciled every 60 s), not streamed over WebSocket. *(My call: fewer moving parts and no new dependency; WebSocket can come later if polling proves slow.)*

**Tech Stack:** Python 3.12+, requests, PyYAML, **cryptography==46.0.3** (new, pinned; Windows wheel confirmed), stdlib sqlite3.

**Spec:** `DESIGN.md` ("How it's built" items 7–8, "Stake and limits", "Phases, gates and timeline"), the risk list in the plan doc, and `STRATEGY.md` 1.1.0 (the quoting rules reused unchanged).

**Scope:** demo only.
- Production trading, arming (`GO LIVE`), the live ledger's reward credits and kappa are Phase 4.
- **Demo facts (checked 2026-11-02):** 1 of 200 open demo markets had a two-sided book, and there are no reward programs. Demo therefore proves plumbing, not profit. When a book is empty, the trader posts "plumbing quotes" far from the touch so every order path still runs.

## Global Constraints

- **Demo only.** `KalshiTrade` accepts only `https://external-api.demo.kalshi.co/trade-api/v2` and `https://demo-api.kalshi.co/trade-api/v2`; any other base URL raises `ValueError`.
- **Keys** come from the environment, or from `.env` at the repo root: `KALSHI_DEMO_KEY_ID` and `KALSHI_DEMO_KEY_PATH`, a path to a PEM file kept outside the repo.
  - Both Ed25519 (PKCS#8) and RSA (PKCS#1/#8) keys are supported, with the type detected from the parsed key.
  - Key material, signatures and key ids never appear in logs, exceptions or the DB.
- **Signing:**
  - The signed string is `str(ts_ms) + METHOD + path`, where the path includes `/trade-api/v2` and excludes any query string.
  - Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE` (base64).
  - RSA uses PSS / SHA-256 / MGF1-SHA-256, with salt length = digest length.
- **Orders:** `POST /portfolio/events/orders` with these fields:
  - `side` `"bid"`/`"ask"` on the YES leg. Our YES bid at p → `bid` @ p. Our NO bid at q → `ask` @ (1 − q).
  - `price` as a 4-decimal dollar string, `count` as a 2-decimal string.
  - `time_in_force: "good_till_canceled"`, `post_only: true`, `cancel_order_on_pause: true`, `self_trade_prevention_type: "taker_at_cross"`.
  - `client_order_id` = uuid4, and `order_group_id` = the market's group.
- **Other endpoints used:**
  - amend: `POST /portfolio/events/orders/{id}/amend`. Its `count` = filled + desired remaining. Changing price or increasing size loses queue position.
  - batch cancel: `DELETE /portfolio/events/orders/batched` with `{"orders":[{"order_id"}]}`
  - cancel all: `DELETE /portfolio/events/orders`
  - resting orders: `GET /portfolio/orders?status=resting`
  - fills: `GET /portfolio/fills?min_ts=…`
  - positions: `GET /portfolio/positions` (`position_fp` > 0 means YES)
  - order groups: `POST /portfolio/order_groups/create` (`contracts_limit`) and `PUT /portfolio/order_groups/{id}/reset`
  - exchange status: `GET /exchange/status` (`trading_active`)
- **Risk rails, from `trade:` config** (stake $300 in demo, mock funds):
  - Worst-case loss per market ≤ 5% ($15), via `make_quotes`.
  - Per event across legs ≤ 10% ($30).
  - Daily loss ≥ 3% ($9): cancel everything and pause until the next UTC day.
  - Weekly loss ≥ 8% ($24): HALT until a person removes `out/trade/HALT`.
  - Order group `contracts_limit` = 2 × quote size.
  - A market's book older than 20 s: cancel that market's orders.
  - `trading_active == false`: cancel all.
  - Reconciliation (positions and resting orders) every 60 s; any mismatch → HALT.
  - Startup **and** shutdown always cancel all resting orders. The bot assumes it is the only thing trading this account.
- **Process separation:** the trader uses `out/trade/{trade.db, trade.log, KILL, HALT}`. It never touches `out/KILL` or the recorder DB.
- **Fault injection** (drills only): `out/trade/FAULT_NET` and `out/trade/FAULT_429` make the client raise or return 429. **The hook is active only when the base URL is a demo URL.**
- **Pinned deps:** add `cryptography==46.0.3` to `requirements.txt`.
- **Commits** end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **A production URL or a production key reaching Phase 3 code.** Expected: refused before any request. Pinned in Task 2 (`test_refuses_production_base_url`).
2. **Credentials leaking into logs or errors** (a 401, a traceback). Expected: no key id, signature or key path in any message. Pinned in Task 2 (`test_errors_never_contain_credentials`).
3. **The loop dying with orders still resting.** Expected: the next `start` cancels all before quoting, and `panic` cancels all even with the loop dead. Pinned in Task 6 (`test_start_cancels_everything_first`) and Task 8 (`test_panic_cancels_without_loop`).
4. **Partial fills, then an amend.** Expected: amend `count` = already filled + desired remaining, never re-buying what filled. Pinned in Task 3 (`test_amend_count_includes_filled`).
5. **Windows clock drift causing 401s.** Expected: the error says to check the system clock, and the loop records an error event rather than crashing. Pinned in Task 2 (`test_401_hints_clock`).

---

### Task 1: Signer and .env loader

**Files:**
- Create: `kxmm/auth.py`, `.env.example`
- Modify: `requirements.txt` (add `cryptography==46.0.3`), `.gitignore` (already ignores `.env`)
- Test: `tests/test_auth.py`

**Interfaces:**
- Produces:
  - `load_env(path: Path) -> dict[str, str]`: parses `KEY=VALUE` lines, skipping blank lines and `#` comments, and strips matching quotes. It never raises on a missing file (returns `{}`).
  - `class Signer`, built with `Signer.from_pem(key_id: str, pem_path: Path)`. Its method `headers(method: str, path: str, ts_ms: int | None = None) -> dict[str, str]` drops any `?query` from `path` before signing.

- [ ] **Step 1: Write failing tests.** Generate an Ed25519 key and an RSA-2048 key in `tmp_path`, using `cryptography`.
  - `test_ed25519_signature_verifies`: `headers("GET", "/trade-api/v2/portfolio/balance?x=1", ts_ms=1700000000000)` verifies against the public key over `b"1700000000000GET/trade-api/v2/portfolio/balance"`.
  - `test_rsa_pss_signature_verifies`: same, verified with PSS(MGF1(SHA256), salt_length=32).
  - `test_headers_names`: the three header names, with the timestamp header equal to `"1700000000000"`.
  - `test_load_env_parses_and_ignores_comments`.
- [ ] **Step 2: Run them.** Run: `.venv/bin/pip install -r requirements.txt && .venv/bin/pytest -q tests/test_auth.py`. Expected: FAIL.
- [ ] **Step 3: Implement `kxmm/auth.py`**, plus `.env.example` with the two variable names and a comment: "demo keys only in Phase 3; key file outside this folder".
- [ ] **Step 4: Run them.** Expected: 4 passed.
- [ ] **Step 5: Commit** with message `feat: request signer (Ed25519/RSA-PSS) and .env loader`.

### Task 2: Demo-only trading client

**Files:**
- Create: `kxmm/kalshi_trade.py`
- Test: `tests/test_kalshi_trade.py`

**Interfaces:**
- Consumes: `Signer` (Task 1); `NotFound`, `Unavailable` and `RateLimiter` from `kxmm/kalshi_public.py`.
- Produces:
  - `DEMO_BASES` tuple.
  - `class TradeError(RuntimeError)`, whose message is never built from request headers.
  - `KalshiTrade(base_url, signer, rate_per_sec=8.0, timeout_s=20, max_attempts=4, session=None, sleep=time.sleep, fault_dir: Path | None = None)`, with property `is_demo`. Methods, all returning parsed JSON:
    - `create_order(ticker, book_side, price, count, client_order_id, order_group_id) -> dict`
    - `amend_order(order_id, ticker, book_side, price, count) -> dict`
    - `batch_cancel(order_ids: list[str]) -> dict`
    - `cancel_all() -> None`
    - `resting_orders(ticker: str | None = None) -> list[dict]`: paginated
    - `fills(min_ts: int) -> list[dict]`: paginated, `limit=1000`
    - `positions() -> list[dict]`: paginated
    - `create_order_group(contracts_limit: int) -> str`
    - `reset_order_group(group_id: str) -> None`
    - `exchange_status() -> dict`
    - `orderbook(ticker) -> dict`: `orderbook_fp`
    - `markets(**filters) -> list[dict]`
    - `take(ticker, book_side, price, count) -> dict`: immediate_or_cancel, not post-only, **drills only**
  - `on_attempt` attribute, like `KalshiPublic`.
  - `_request(method, path, params=None, body=None)`:
    - signs `"/trade-api/v2" + path` and retries 429/5xx/network errors with backoff `2**attempt`.
    - A 401 raises `TradeError("401 unauthorized: check the API key and the system clock")`.
    - A 404 raises `NotFound`. Any other 4xx raises `TradeError` carrying the server's `code`/`message`.
    - Fault hook: if `fault_dir` is set **and** `is_demo`, an existing `FAULT_NET` raises `requests.ConnectionError`, and an existing `FAULT_429` acts as a 429 response.

- [ ] **Step 1: Write failing tests** with a fake session recording `(method, url, params, json, headers)`:
  - `test_refuses_production_base_url`: `https://external-api.kalshi.com/trade-api/v2` and `https://api.elections.kalshi.com/trade-api/v2` both raise `ValueError`.
  - `test_create_order_body`: `create_order("K","ask",0.52,10,"cid","g1")` posts to `/portfolio/events/orders` with the JSON `{"ticker":"K","side":"ask","price":"0.5200","count":"10.00","time_in_force":"good_till_canceled","post_only":true,"cancel_order_on_pause":true,"self_trade_prevention_type":"taker_at_cross","client_order_id":"cid","order_group_id":"g1"}`.
  - `test_signed_path_includes_prefix_not_query`: the signer saw `/trade-api/v2/portfolio/fills` for `fills(min_ts=5)`.
  - `test_batch_cancel_and_amend_bodies`: exact paths, methods and bodies, with the amend count as `"7.00"`.
  - `test_fills_paginates`.
  - `test_retries_429_then_succeeds`.
  - `test_401_hints_clock`.
  - `test_errors_never_contain_credentials`: a 401 and a 500-exhaustion message contain neither the key id nor the signature header value nor the key path.
  - `test_fault_files_only_on_demo`: with `FAULT_NET` present, a call raises `Unavailable`; with `FAULT_429` present, it retries then raises `Unavailable`. (A non-demo client can't be built, so this is demo-only by construction; assert `is_demo`.)
- [ ] **Step 2: Run them.** Run: `.venv/bin/pytest -q tests/test_kalshi_trade.py`. Expected: FAIL.
- [ ] **Step 3: Implement `kxmm/kalshi_trade.py`** per the Interfaces.
- [ ] **Step 4: Run them.** Expected: 9 passed.
- [ ] **Step 5: Commit** with message `feat: demo-only signed trading client`.

### Task 3: Order translation and diffing (pure)

**Files:**
- Create: `kxmm/orders.py`
- Test: `tests/test_orders.py`

**Interfaces:**
- Consumes: `Quotes` (quoting), `tick_size` (rewards).
- Produces:
  - `to_book(side: str, price: float) -> tuple[str, float]`: `"yes"` @ p → `("bid", p)`; `"no"` @ q → `("ask", round(1 − q, 4))`.
  - `LiveOrder` dataclass: `order_id`, `side` (`"yes"`/`"no"`, in our terms), `price` (our terms), `remaining: float`, `filled: float`.
  - `Action` dataclass: `kind` (`"place"`, `"amend"` or `"cancel"`), `side`, `price: float | None`, `size: float`, `order_id: str | None`, `amend_count: float | None`.
  - `plan_actions(q: Quotes | None, live: dict[str, LiveOrder], price_ranges: list[dict]) -> list[Action]`. For each side:
    - no target → cancel the live order
    - no live order → place
    - price differs by ≥ 1 tick → amend
    - remaining differs from the target size → amend with `amend_count = filled + target size`
    - otherwise nothing

- [ ] **Step 1: Write failing tests**
  - `test_to_book_maps_no_bids_to_yes_asks`: `to_book("no", 0.48) == ("ask", 0.52)`.
  - `test_place_when_nothing_live`.
  - `test_cancel_when_quote_pulled`.
  - `test_no_action_when_unchanged`.
  - `test_amend_on_one_tick_move`.
  - `test_amend_count_includes_filled`: a live order with remaining 4 and filled 6, against a target size of 10, gives `amend_count == 16`.
- [ ] **Step 2: Run them.** Run: `.venv/bin/pytest -q tests/test_orders.py`. Expected: FAIL.
- [ ] **Step 3: Implement `kxmm/orders.py`** per the Interfaces.
- [ ] **Step 4: Run them.** Expected: 6 passed.
- [ ] **Step 5: Commit** with message `feat: quote-to-order diffing in Kalshi's YES-leg terms`.

### Task 4: P&L, loss stops, event cap and reconciliation (pure)

**Files:**
- Create: `kxmm/risk.py`
- Modify: `config.yaml` (add the `trade:` section)
- Test: `tests/test_risk.py`

**Interfaces:**
- Produces the `cfg["trade"]` keys:
  - `base_url: https://external-api.demo.kalshi.co/trade-api/v2`
  - `stake_usd: 300`, `quote_size: 1`
  - `max_event_loss_frac: 0.10`, `daily_loss_frac: 0.03`, `weekly_loss_frac: 0.08`
  - `group_limit_mult: 2`
  - `book_poll_s: 5`, `stale_book_s: 20`, `status_poll_s: 30`, `reconcile_s: 60`
  - `markets: []` (empty = auto-pick), `max_markets: 3`
  - `plumbing_bid: 0.03`, `plumbing_ask: 0.97`, `churn_s: 600`
  - `demo_target: 10`
- Produces:
  - `Book-keeping` class `Pnl`, with `apply_fill(ticker, book_side, yes_price, count, fee)`, `position(ticker) -> float` and `equity(marks: dict[str, float]) -> float` (cash + Σ position × mark). A bid buys YES: cash −= p·c. An ask sells YES: cash += p·c. Fees are subtracted.
  - `loss_state(equity_now, equity_day_start, equity_week_start, tcfg) -> str`: `"ok"`, `"pause_day"` or `"halt"`.
  - `event_room(positions: dict[str, float], marks: dict[str, float], event_of: dict[str, str], ticker: str, tcfg) -> float`: the dollars of worst-case loss still allowed in that ticker's event.
  - `reconcile(local_pos: dict[str, float], remote_pos: dict[str, float], local_ids: set[str], remote_ids: set[str]) -> list[str]`: human-readable mismatches; position tolerance 0.005.

- [ ] **Step 1: Write failing tests**
  - `test_pnl_round_trip`: bid 10 @ 0.40, ask 10 @ 0.45 and fees 0.01 + 0.01 give position 0 and equity 0.48.
  - `test_loss_state_thresholds`: a stake of 300 and day-start equity of 0. Equity −8.99 gives `ok`; −9.00 gives `pause_day`; a week-start delta of −24 gives `halt`.
  - `test_event_room_counts_all_legs`: two legs of one event, each long 20 at mark 0.5 ($10 worst case each), leave $10 of room.
  - `test_reconcile_reports_each_mismatch`: one position off by 1 and one unknown remote order give 2 lines.
- [ ] **Step 2: Run them.** Run: `.venv/bin/pytest -q tests/test_risk.py`. Expected: FAIL.
- [ ] **Step 3: Implement `kxmm/risk.py`** and the config section.
- [ ] **Step 4: Run them.** Expected: 4 passed.
- [ ] **Step 5: Commit** with message `feat: P&L, loss stops, event cap and reconciliation`.

### Task 5: Trade ledger

**Files:**
- Create: `kxmm/tradedb.py`
- Test: `tests/test_tradedb.py`

**Interfaces:**
- Produces: `connect(path) -> sqlite3.Connection`, WAL, with tables:
  - `fills(fill_id PRIMARY KEY, ts, ticker, book_side, yes_price, count, fee, order_id)`
  - `events(id, ts, kind, detail)`, where `kind` is one of `start`, `stop`, `error`, `halt`, `pause`, `reconcile_ok`, `reconcile_bad`, `stale` or `drill`
  - `drills(id, ts, name, passed INTEGER, detail)`
  - `kv(k, v)`
- Produces functions:
  - `add_fills(con, rows: list[dict]) -> list[dict]`: returns only the new rows (dedup by `fill_id`).
  - `event(con, ts, kind, detail="")`
  - `drill_result(con, ts, name, passed, detail)`
  - `heartbeat(con, ts)`, `kv_get` and `kv_set`

- [ ] **Step 1: Write failing tests**
  - `test_add_fills_returns_only_new`.
  - `test_events_and_drills_round_trip`.
- [ ] **Step 2: Run them.** Run: `.venv/bin/pytest -q tests/test_tradedb.py`. Expected: FAIL.
- [ ] **Step 3: Implement `kxmm/tradedb.py`** per the Interfaces.
- [ ] **Step 4: Run them.** Expected: 2 passed.
- [ ] **Step 5: Commit** with message `feat: trade ledger`.

### Task 6: Trader loop (demo)

**Files:**
- Create: `kxmm/trader.py`, `kxmm/tradeloop.py` (entry point `python -m kxmm.tradeloop --log-file …`)
- Test: `tests/test_trader.py`

**Interfaces:**
- Consumes: Tasks 2–5; `make_quotes` and `Quotes`; `EwmaVol` / `microprice` / `logit`; `Program` and `Meta` (with a synthetic program: `Program(start=0, end=inf, daily_pool=0, target=trade.demo_target, discount=0.5)`); `keep_awake`.
- Produces: `Trader(cfg, client, con, root: Path, clock=time.time, sleep=time.sleep, awake=keep_awake)`. Methods:
  - `start() -> None`: `cancel_all`, records a `start` event, picks markets (`trade.markets` or auto: up to `max_markets` open binary non-MVE demo markets with ≥ 3 days to close, two-sided books first), and creates one order group per market.
  - `cycle(now) -> None`, in this order:
    1. KILL / HALT check.
    2. Exchange status (every `status_poll_s`); if not active, cancel all.
    3. For each market: book (stale → cancel that market's orders and record a `stale` event), then vol.
    4. Target quotes: `make_quotes(…, scfg)` with `scfg = {**cfg["sim"], "quote_size": trade.quote_size, "stake_usd": trade.stake_usd}`. The quoting rules stay STRATEGY.md's; only size and stake come from `trade:`. If that gives None and the book is not crossed, use plumbing quotes: YES bid at `plumbing_bid`, NO bid at `1 − plumbing_ask`, size 1, shifted 1 tick every `churn_s` to exercise amend.
    5. Event cap: zero the adding side when `event_room ≤ 0`.
    6. Loss state: `pause_day` cancels all and skips quoting until the next UTC day; `halt` writes HALT.
    7. `plan_actions` → execute; each action's exception is recorded as an `error` event, and the cycle continues.
    8. Fills since the last poll → `Pnl`.
    9. Every `reconcile_s`, reconcile; a mismatch records `reconcile_bad` and writes HALT with the reasons.
    10. Heartbeat.
  - `run() -> None`: runs `start()` and loops `cycle()`. KILL means cancel all, write `stopped`, delete KILL and return. HALT (file present) means cancel all and keep heartbeating without quoting. An unexpected exception records an `error` event and continues.

- [ ] **Step 1: Write failing tests** with a `FakeTradeClient` that keeps resting orders, positions and fills in memory, and can be told to fill an order or misreport a position.
  - `test_start_cancels_everything_first`: pre-existing orders are gone after `start()`.
  - `test_places_two_sided_quotes_on_a_live_book`: one `bid` and one `ask`.
  - `test_plumbing_quotes_on_empty_book_and_churn`: an empty book gets bid 0.03 / ask 0.97, and after `churn_s` one amend is made.
  - `test_fill_updates_position_and_requotes`.
  - `test_reconcile_mismatch_halts_and_cancels`: HALT file exists with the reason text, and there are no resting orders.
  - `test_daily_loss_pauses_until_next_day`: a $9 loss means no quoting until 00:00 UTC.
  - `test_stale_book_cancels_that_market`.
  - `test_kill_cancels_all_and_exits`.
  - `test_action_error_is_recorded_not_fatal`.
- [ ] **Step 2: Run them.** Run: `.venv/bin/pytest -q tests/test_trader.py`. Expected: FAIL.
- [ ] **Step 3: Implement `kxmm/trader.py` and `kxmm/tradeloop.py`.** Logging is the same as `kxmm/loop.py`; reuse `configure_logging`.
- [ ] **Step 4: Run them.** Expected: 9 passed.
- [ ] **Step 5: Commit** with message `feat: demo trader loop with safety stops`.

### Task 7: Drills

**Files:**
- Create: `kxmm/drills.py`
- Test: `tests/test_drills.py`

**Interfaces:**
- Consumes: `KalshiTrade`, `tradedb`, and the trader's files in `out/trade/`.
- Produces: `run_drill(name: str, client, con, root: Path, clock, sleep) -> tuple[bool, str]`, which also records `drills` rows. The drills:
  - **`orders`** (loop not needed): place a post-only 1-lot at the plumbing bid → amend its price up 1 tick → amend its count to 2 → cancel. Each step is confirmed in `resting_orders()` (price, remaining count, then absence).
  - **`group`:** create a group with limit 1, place 2 far 1-lots in it, trigger by `PUT …/trigger`, and assert both are cancelled; then reset.
  - **`fill`:** find a demo market with a resting ask that isn't ours, `take` 1 YES at that ask, and assert the fill appears in `fills()` and `positions()` moves +1.
  - **`kill` / `halt`** (loop running): write the file and assert that within 120 s the resting orders are empty, plus the loop's state (exited for kill; heartbeat alive for halt). HALT is then removed.
  - **`network` / `ratelimit`** (loop running): create `FAULT_NET` or `FAULT_429` for 60 s, then remove it. Assert the heartbeat stayed fresh, no unhandled `error` events were added, and the next reconcile is `reconcile_ok`.
  - **`reconcile`** (loop running): write `out/trade/FAULT_PHANTOM`, which makes the loop add a phantom +1 to its local position on the next reconcile. Assert HALT appears with "position" in its reason, then clear both files.

- [ ] **Step 1: Write failing tests** for `orders`, `group`, `fill` and `reconcile` against the in-memory `FakeTradeClient` (moved to `tests/fakes.py`). The loop-based drills are tested with an in-process `Trader` on a fake clock: `test_kill_drill_passes`, `test_network_drill_passes`. 6 tests in all.
- [ ] **Step 2: Run them.** Run: `.venv/bin/pytest -q tests/test_drills.py`. Expected: FAIL.
- [ ] **Step 3: Implement `kxmm/drills.py`.** Also add `trigger_order_group(group_id)` (`PUT /portfolio/order_groups/{id}/trigger`) to `KalshiTrade`, and handle `FAULT_PHANTOM` in `Trader` (demo only).
- [ ] **Step 4: Run them.** Expected: 6 passed.
- [ ] **Step 5: Commit** with message `feat: demo drills for every safety path`.

### Task 8: `trade` commands, Gate B status, README

**Files:**
- Modify: `kxmm/cli.py`, `README.md`
- Test: `tests/test_trade_cli.py`

**Interfaces:**
- Consumes: everything above; `spawn_command` / `spawn_kwargs` (reused with module `kxmm.tradeloop`).
- Produces these `python kxmm.py trade …` commands:
  - `start`: refuses without demo keys, printing which variable is missing and pointing to the README.
  - `stop`: via `out/trade/KILL`.
  - `status`: loop state; the last 24 h of fills, errors, halts and reconciles; positions; equity; drill results; and the Gate B verdict.
  - `panic`: `cancel_all` via the API, then write HALT and KILL. It works with the loop dead.
  - `drill <name>`
- Produces: `gate_b(con, now, tcfg) -> tuple[bool, list[str]]`. It passes when:
  - there are 5 consecutive UTC days, ending yesterday or today, each with ≥ 20 h of trader heartbeat
  - those days have 0 `error` and 0 `reconcile_bad` events
  - each drill (`orders`, `group`, `fill`, `kill`, `halt`, `network`, `ratelimit`, `reconcile`) passed at least once in that window

- [ ] **Step 1: Write failing tests**
  - `test_start_without_keys_explains`.
  - `test_panic_cancels_without_loop` (fake client injected).
  - `test_gate_b_needs_clean_days_and_drills`: 4 clean days gives NOT YET with reason "4 of 5 clean days". 5 clean days with all drills gives PASS. One `error` event resets the run.
- [ ] **Step 2: Run them.** Run: `.venv/bin/pytest -q tests/test_trade_cli.py`. Expected: FAIL.
- [ ] **Step 3: Implement the commands and `gate_b`**, and add a README section "Phase 3: demo trading". It covers making a demo account at demo.kalshi.co, generating an **Ed25519** API key **on the machine that runs the bot**, saving the `.pem` outside the folder, filling `.env`, and the command and drill list.
- [ ] **Step 4: Run them.** Expected: 3 passed, and the full suite passes.
- [ ] **Step 5: Commit** with message `feat: trade commands and Gate B verdict`.

### Task 9: Demo smoke run

- [ ] **Step 1:** If `KALSHI_DEMO_KEY_ID` is set on this machine, run the following and report each result:
  - `python kxmm.py trade drill orders`, then `group`, then `fill`
  - `python kxmm.py trade start`, wait 10 minutes, then `drill kill`
  - `python kxmm.py trade status`

  Expected: every drill PASS; no `error` events; `GATE B: NOT YET — 0 of 5 clean days`.

  If no key is set here, write the same command list into the hand-off for the Windows machine.
- [ ] **Step 2: Commit** any fixes this step needed (each with its own test first), with message `fix: <what the demo showed>`.

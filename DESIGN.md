# kxmm: Kalshi market-making test (design)

Draft for approval · 2026-10-30 · moves to `~/Desktop/Project/kxmm/DESIGN.md` once approved.
Source plan: the "Prediction-Market Market-Making Plan" doc.

## Intent

Answer one question with real money at small size: **does a maker-only bot on Kalshi make money consistently enough that it isn't luck?** Everything else (scaling, Polymarket US, polish) waits on that answer.

The answer has to come from real fills. lpfarm already showed a strong edge on paper (Polymarket, Jul–Aug 2026), but its real-money twin never placed an order. A paper result that never meets the order book is exactly what this project must not stop at.

## The test (decided with you)

1. **Primary:** total P&L per market-day = trading P&L (spread captured − markout − fees) + liquidity rewards. Interest is excluded: Kalshi pays 3.50% APY on idle cash too, so it is the benchmark, not bot profit.
2. **Reported beside it:** trading P&L alone, which answers "does it survive if rewards end?"
3. **"Beyond chance":** group results by calendar day (markets on the same day move together) and bootstrap over days. Pass = the one-sided lower 97.5% bound of mean daily P&L is above zero (about t ≥ 2).
4. **Rewards-only profit** (primary passes, trading-only ≤ 0): conditional yes. Go live, but stay at small size until Kalshi renews its rewards past Jan 1, 2027. *(My call; you didn't object.)*

The test, thresholds and sample-size target go into `STRATEGY.md` before any live order and are not changed after data arrives (house void policy).

## Goals, non-goals, anti-goals

- **Goals:** a verdict from real Kalshi fills before the reward terms expire on Jan 1, 2027. Reuse proven pieces. A written pass/fail at every gate.
- **Non-goals:** Polymarket US (after Gate C); speed or HFT; a model-based fair value; cloud hosting; UI beyond the house dashboard.
- **Anti-goals:** a verdict that is a paper artifact; thresholds tuned after seeing results; any real order you didn't arm; losses beyond the stake; anything Kalshi's rulebook bars (spoofing, layering, quote stuffing, flickering quotes, wash or self trades).

## What the review of existing code found

| Code | Verdict | Use |
|---|---|---|
| `lpfarm` paper test | +$3.25 per market-day trading-only (t = 7.6, 24 of 25 days positive). Two biases checked: 156 fills (13%) were never scored because their market left the watch list; re-marked from public price history they add +$1,022, so they don't explain the result. The fill rule uses price priority for trades through the quote (sound) but puts you first in the queue at your own price (optimistic). Rewards are an estimate with a 50% haircut, never checked against a payout. | Strategy lessons; per-fill statistics for sample sizing |
| `lpfarm` lplive | 439 dry-run orders, 0 real. Its shadow replay got 20 fills in 3 weeks, too few to judge. | Pattern for arming live (`GO LIVE`), HALT/KILL files, risk rails |
| `refarb` KalshiExecutor | Signing (RSA-PSS over timestamp + method + path) matches Kalshi's docs but was never exercised. Integer-cent prices can't hit 0.1¢ tail ticks; no post-only, order groups or self-trade prevention; reads the key file on every request. Its `--demo` flag is a synthetic signal, not Kalshi's demo exchange. | Port the signing only; write a new order client |
| `kxsurv` KalshiPublic | Solid: local rate limiter, pagination that fails closed, live + historical trade union. | Copy as the public-data client |

Facts checked live on Kalshi today:
- Markets, order books, trades (with taker side) and incentive programs are all public. **Recording and simulation need no account or key.**
- There are 5,846 active liquidity programs across 559 series. The median pool is $100 (`period_reward` ÷ 10,000), programs usually run about a week, the target size is 1,000 contracts, and the discount is 50% per tick.

## How it's built (my calls unless noted)

- **Where and what:** built and tested on the Mac in `~/Desktop/Project/kxmm`, laid out like lpfarm and wxedge: a venv, pinned `requirements.txt` (requests, PyYAML, flask, cryptography, pytest; all ship Windows wheels), `config.yaml` holding every knob, SQLite in WAL mode in `out/`, and `STRATEGY.md` with version bumps. The dashboard is on **:5120**, since 5111–5119 are taken on the Mac.
- **Where it runs (yours): a Windows machine, 24/7.** lplive's `.ps1` was never run on Windows, so kxmm keeps the launcher logic in Python, where it can be tested here:
  - `python kxmm.py start|stop|status|gui|log|test|panic`, with thin `kxmm.ps1` and `kxmm.sh` wrappers.
  - `stop` writes `out/KILL`, which the loop checks each cycle before cancelling and exiting, because Windows can't deliver SIGTERM.
  - While running, the loop asks Windows to stay awake (`SetThreadExecutionState`); on the Mac it uses `caffeinate`. Set the power plan to never sleep as well (lpfarm's README-WINDOWS steps).
  - Copy the project without `.venv` and `.env`, then rebuild the venv on Windows.
  - Create the Kalshi API keys on the Windows machine itself, so the private key never travels.
  - **One machine at a time:** the bot cancels the account's orders on start and stop, so two copies would fight.
- **Components:**
  1. `kalshi_public.py`: copied from kxsurv's client, plus orderbook and incentive-program calls.
  2. **Recorder:** every hour it refreshes incentive programs and candidate markets. Every 10 s it fetches the order book for each selected market (up to 40), storing a book only when it changes. Every 30 s it fetches new trades per market. It stays at or under 8 requests a second.
  3. **Selector:** the plan's favor/avoid table as filters. Requires an active liquidity program, a mid of 10–90¢ and at least 3 days to close. Excludes combo (MVE) markets, maker-fee series where the reward per contract doesn't beat the fee, in-play sports and scheduled catalyst windows. Ranks by expected reward per dollar of collateral.
  4. **Quoting engine** (pure functions, unit-tested): the plan's log-odds Avellaneda–Stoikov. Microprice fair value, inventory skew, placement at the reward reference price, the tick grid from each market's `price_ranges`, size shrinking near the inventory cap, and catalyst blackouts.
  5. **Fill simulator:** stricter than lpfarm. A trade through your price fills you up to its size. A trade at your price fills you only beyond the displayed queue ahead of you when you joined. Markouts at 1, 10 and 60 minutes and at settlement. Reward accrual follows Kalshi's formula on each recorded book, with the snapshot approximation stated.
  6. **Report:** per-market-day attribution, the test above, and the dashboard.
  7. **Order client** (Phase 3 on): signing ported from refarb. Create Order V2 with dollar-string prices, post-only, self-trade prevention and `cancel_order_on_pause`. Order groups with a 15-second fill limit. Batch cancel and amend, a WebSocket feed for fills and positions, reconciliation every minute, HALT and KILL files, and the plan's loss stops.
  8. **Live ledger:** real fills and actual reward credits from portfolio endpoints, plus kappa (actual ÷ estimated rewards), the check lpfarm never got.
- **Keys:** you create the demo and production API keys. They live in `.env` (chmod 600) as a key id and a path to the key file. The bot never prints them, and the dashboard never sees them.
- **Arming live:** the same as lplive. Set `mode: live` in config, run `./kxmm.sh start-live`, and type `GO LIVE`.

## Stake and limits (worked example: $300)

The stake is your call: only money you can afford to lose. This section is test mechanics, not financial advice. The test counts fills, not dollars. How many fills you get depends on how many markets you quote and where, not on order size, so a smaller stake reaches a verdict about as fast and puts less at risk. Each limit stays a fixed share of the stake, as in the plan.

| Limit | Share of stake | At $300 |
|---|---|---|
| Quote size | — | 10 contracts a side |
| Markets quoted at once | — | 5 |
| Resting collateral | — | about $50 (each side pair ties up about $1 per contract) |
| Worst-case loss per market | 5% | $15 (an inventory cap of about 25–30 contracts at mid prices) |
| Per event, all legs | 10% | $30 |
| Daily loss stop | 3% | $9: cancel all until the next day |
| Weekly loss stop | 8% | $24: halt until you review |

Floors and trade-offs:
- **Fees.** Kalshi rounds each fee up to the next cent, so tiny orders overpay. A 1-contract taker exit at 50¢ pays 2¢ instead of 1.75¢. Quotes stay at 10 contracts and maker-fee series stay excluded.
- **Rewards.** Your share of a pool grows with your size, so reward per dollar stays about the same. The dollars are just small. Phase 1 checks that Kalshi sets no minimum order size for rewards.
- **What a small test can't tell you.** Small quotes get filled by small trades. At larger size you meet more of the big sweeps that move against you, so a pass at $300 proves the edge at small size only. Scaling then re-tests at each step, at most doubling capital per month as in the plan.
- **Below about $200** the bot would drop to 3–4 markets, which slows the test.

## Phases, gates and timeline

The doc's timeline ran live trading from Dec 14 to Jan 10, straddling both the holidays and the Jan 1 reward expiry. This version finishes the live test while rewards are still in place. Recording and simulation need no keys, so they start Monday.

| Phase | Dates | What you do | Pass to move on |
|---|---|---|---|
| 1. Build recorder, start recording | Nov 2–4 | Copy kxmm to the Windows machine, run setup, `start` | 24 h of recording on 20+ markets, no gap over 5 min |
| 2. Simulator and quoting engine, run on the recording | Nov 4–16 | Nothing | **Gate A, Mon Nov 16:** simulated net > 0 in at least 3 markets over 10+ recorded days, under the strict queue rule. Live sample-size target written into STRATEGY.md |
| Order client (built alongside Phase 2) | Nov 9–16 | Make a Kalshi demo account and demo API key | Unit tests pass |
| 3. Demo exchange | Nov 17–24 | Nothing | **Gate B, Tue Nov 24:** 5 straight days with no unhandled errors and clean reconciliation; kill switch, reconnect and 429 drills pass |
| 4. Live, small size | Nov 30–Dec 28 (after Thanksgiving) | Fund Kalshi with your stake (limits scale with it), create a production key on the Windows machine, type `GO LIVE` | Interim look Mon Dec 14: stop early only if t ≥ 3 either way. **Gate C, Mon Dec 28:** the test above |
| Extension, only if the sample is short | up to Mon Jan 25 at most | Nothing | Gate C on the full sample (note: rewards may lapse Jan 1) |

**Sample size.** lpfarm's paper fills (+3.6% mean, 15.8% s.d. per fill) would need about 76 independent fills for t ≈ 2. A real edge half that size needs about 300 fills; a quarter needs about 1,200. Gate A replaces these guesses with Kalshi's own simulated fill rate and edge. That sets the fill target, which decides whether four live weeks are enough or the extension is needed.

## Reward and rate facts (checked 2026-10-30)

- **`period_reward`** is "total reward for the period in centi-cents" (÷ 10,000 = dollars). It is read as the total for the program's start-to-end window, so daily pool = `period_reward` ÷ 10,000 ÷ program days. Kalshi's help page also mentions "daily rewards of $1 to $1,000 per market", which leaves some doubt. Real payouts in Phase 4 (kappa) settle it.
- **Scoring:**
  - Kalshi samples the book once per second, at a random moment.
  - The **reference price** is the first level, walking down from the best bid, where cumulative resting size reaches one fifth of the target size.
  - Orders at or better than the reference price score 1.0×. Orders below it score the discount factor raised to the number of ticks below.
  - Only orders that help reach the target size on their side qualify. A side whose total depth is below the target scores nothing.
  - A snapshot counts only when both sides meet the target. Each side is normalized on its own, so a snapshot is worth up to 2.0.
  - No minimum order size is stated.
- **Read limit:** the Basic tier allows 200 tokens a second at 10 tokens a request, about 20 requests a second when authenticated. The public limit isn't documented, so the recorder stays at or under 8 a second.
- **Order book shape:** `orderbook_fp.yes_dollars` and `no_dollars` are bid ladders of `[price, size]` strings, lowest price first. The YES ask is 1 − the best NO bid.
- **Series** carry `category` and `fee_type`. `quadratic_with_maker_fees` marks a maker-fee series; for example, KXNFLGAME has it.

## Left to the builder

- Schema, module boundaries, test fixtures and dashboard layout.

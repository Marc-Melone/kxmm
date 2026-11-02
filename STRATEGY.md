Strategy version: 1.0.0 (pre-registered 2026-11-02, before any recorded day was simulated)

# kxmm strategy — pre-registered rulebook

The numbers here are the ones in `config.yaml` → `sim:`. A test fails if the
version line and the config disagree.

## Thesis

lpfarm showed a strong maker edge on Polymarket paper (+$3.25 per market-day,
t = 7.6) but never met a real fill. This simulator is the Kalshi paper step:
it replays recorded books and trades through the rules below, with a fill
model stricter than lpfarm's. Gate A decides whether the strategy earns a
live test; live fills, not this simulation, decide the question.

## Quoting

- **Fair value:** microprice of the top of book, in log-odds
  (x = ln(p / (1 − p))).
- **Volatility:** EWMA of squared log-odds changes per second, half-life 3,600 s.
  The recorder stores books only when they change, so until the first change
  the variance is taken as 0.
- **Reservation:** x_r = x̂ − (position / 10) × γ × σ² × τ, with γ = 0.1 and
  τ = min(86,400 s, time to blackout).
- **Risk half-width:** δ = ½ × γ × σ² × τ.
- **Prices:** each side bids at the reward reference price *or further from
  mid*, whichever the reservation ± δ gives. Bids are floored to the market's
  tick and never cross the opposite best (post-only).
- **Size:** 10 contracts a side, cut so worst-case loss stays ≤ 5% of a $300
  stake ($15): long size ≤ 15 / mid − position, short mirrored with 1 − mid.
  At the cap the side that would add is pulled.
- **Requotes:** only when a target price moves ≥ 1 tick or the size changes.
- **Blackout:** no quotes within 3 days of `close_time` or
  `occurrence_datetime`, whichever is earlier, and none inside recorded gaps
  (resting orders are cancelled at a gap's start).
- **Not used in 1.0.0:** Avellaneda–Stoikov's fill-intensity term k. It is
  fitted from simulated fills at Gate A.

## Fill model (strict queue)

- A trade through our price fills us up to its size.
- A trade at our price fills us only beyond the displayed queue that was
  ahead of us when we joined. That queue only shrinks: by at-price trades,
  and down to the displayed size when the book shows less.
- A new price puts us at the back of the queue.
- A taker buying NO hits YES bids; a taker buying YES hits NO bids at 1 − YES
  price.
- Fees: maker fee round-up(0.0175 × C × P × (1 − P)) on series with maker
  fees; we never take liquidity.

## Rewards

- Kalshi's scoring (DESIGN.md "Reward and rate facts") with our resting
  orders included in the book.
- Accrued between book changes, only while both sides meet the target size.
- Daily pool = `period_reward` ÷ 10,000 ÷ program days.
- Reported raw and × 0.5. **The primary metric uses the haircut.**

## Metrics and the test

- Markouts at 60 s, **600 s (primary)** and 3,600 s, plus settlement, in YES
  space: a YES-bid fill at p earns (mid − p) per contract; a NO-bid fill at q
  earns ((1 − q) − mid).
- **Primary per market-day** = Σ (600 s markout − fee) + reward × 0.5.
  Interest is excluded. Trading-only is reported beside it.
- **Beyond chance:** sum market-days into calendar days (UTC), bootstrap the
  mean daily total (10,000 resamples, seed 7). Pass = 2.5th percentile > 0.

## Gate A

- **Pass:** simulated net > 0 in at least 3 markets, over at least 10
  recorded days.
- The report also gives the per-fill mean and s.d. of the 600 s markout
  return, `fills_needed = ceil((2 × sd / mean)²)`, and the implied live days
  at 5 markets. That target is written into "Gate A result" below before any
  live order.

## Void policy

Changing any rule or number above bumps the version and starts a new record
segment. Tuning may use only the first 5 recorded days.

## Gate A result

(empty until Gate A)

## Changelog

(none)

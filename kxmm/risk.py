"""Live P&L from fills, loss stops, the per-event cap and reconciliation.

Positions are signed YES contracts (Kalshi's position_fp: + YES, - NO).
A YES-leg bid fill buys YES; an ask fill sells YES.
"""
from __future__ import annotations


class Pnl:
    def __init__(self):
        self.cash = 0.0
        self._pos: dict[str, float] = {}

    def apply_fill(self, ticker: str, book_side: str, yes_price: float, count: float,
                   fee: float) -> None:
        sign = 1.0 if book_side == "bid" else -1.0
        self._pos[ticker] = self._pos.get(ticker, 0.0) + sign * count
        self.cash += -sign * yes_price * count - fee

    def apply_cash(self, book_side: str, yes_price: float, count: float, fee: float) -> None:
        """A fill whose position change is already in a seeded snapshot: cash only."""
        sign = 1.0 if book_side == "bid" else -1.0
        self.cash += -sign * yes_price * count - fee

    def position(self, ticker: str) -> float:
        return self._pos.get(ticker, 0.0)

    def positions(self) -> dict[str, float]:
        return dict(self._pos)

    def equity(self, marks: dict[str, float]) -> float:
        return self.cash + sum(p * marks.get(t, 0.0) for t, p in self._pos.items())


def worst_case(position: float, mark: float) -> float:
    return position * mark if position >= 0 else -position * (1 - mark)


def loss_state(equity_now: float, equity_day_start: float, equity_week_start: float,
               tcfg: dict) -> str:
    stake = tcfg["stake_usd"]
    if equity_now - equity_week_start <= -tcfg["weekly_loss_frac"] * stake + 1e-9:
        return "halt"
    if equity_now - equity_day_start <= -tcfg["daily_loss_frac"] * stake + 1e-9:
        return "pause_day"
    return "ok"


def event_room(positions: dict[str, float], marks: dict[str, float], event_of: dict[str, str],
               ticker: str, tcfg: dict) -> float:
    ev = event_of.get(ticker)
    used = sum(worst_case(p, marks.get(t, 0.5)) for t, p in positions.items()
               if event_of.get(t) == ev)
    return tcfg["max_event_loss_frac"] * tcfg["stake_usd"] - used


def reconcile(local_pos: dict[str, float], remote_pos: dict[str, float],
              local_ids: set[str], remote_ids: set[str]) -> list[str]:
    out = []
    for t in sorted(set(local_pos) | set(remote_pos)):
        a, b = local_pos.get(t, 0.0), remote_pos.get(t, 0.0)
        if abs(a - b) > 0.005:
            out.append(f"{t}: local position {a:g} vs Kalshi {b:g}")
    for oid in sorted(remote_ids - local_ids):
        out.append(f"order {oid} rests on Kalshi but not locally")
    for oid in sorted(local_ids - remote_ids):
        out.append(f"order {oid} is tracked locally but not resting on Kalshi")
    return out


def seed_positions(pnl: Pnl, positions: dict[str, float]) -> None:
    """Start from what Kalshi already holds, so reconciliation agrees from the first check."""
    for t, p in positions.items():
        pnl._pos[t] = float(p)

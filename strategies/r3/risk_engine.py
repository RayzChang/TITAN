"""R3 Sprint 3 per-trade risk planning."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config_loader import R3Config


@dataclass(frozen=True)
class RiskPlan:
    symbol: str
    direction: str
    equity: float
    base_risk_pct: float
    risk_multiplier: float
    final_risk_pct: float
    risk_amount: float
    entry_price: float
    stop_price: float
    stop_loss_pct: float
    position_notional: float
    quantity: float
    max_loss: float
    approved: bool
    rejection_reasons: list[str] = field(default_factory=list)
    metrics_snapshot: dict[str, Any] = field(default_factory=dict)


class RiskEngine:
    """Pure risk-plan builder. It does not place orders or mutate positions."""

    def __init__(self, cfg: R3Config):
        self.cfg = cfg
        self._profile = cfg.risk.profiles[cfg.risk.active_profile]
        self.base_risk_pct = float(self._profile["risk_per_trade_pct"]) / 100.0
        self.max_total_open_risk_pct = (
            float(self._profile["max_total_open_risk_pct"]) / 100.0
        )
        self.daily_loss_limit_pct = float(self._profile["daily_loss_limit_pct"])

    def build_plan(
        self,
        symbol: str,
        direction: str,
        equity: float,
        entry_price: float,
        stop_price: float,
        risk_multiplier: float = 1.0,
        current_open_risk_pct: float = 0.0,
    ) -> RiskPlan:
        """Build a single-trade risk plan.

        `entry_price` is the intended limit price. Q26 requires quantity,
        stop-loss percentage, and R calculations to use this limit price.
        Percent arguments in this method are fractions, e.g. 0.0075 is 0.75%.
        """
        rejection_reasons: list[str] = []

        if equity <= 0:
            rejection_reasons.append("INVALID_EQUITY")
        if entry_price <= 0 or stop_price <= 0:
            rejection_reasons.append("INVALID_PRICES")
        if direction not in {"long", "short"}:
            rejection_reasons.append("INVALID_DIRECTION")
        if risk_multiplier <= 0:
            rejection_reasons.append("INVALID_RISK_MULTIPLIER")
        if direction == "long" and stop_price >= entry_price:
            rejection_reasons.append("LONG_STOP_NOT_BELOW_ENTRY")
        if direction == "short" and stop_price <= entry_price:
            rejection_reasons.append("SHORT_STOP_NOT_ABOVE_ENTRY")

        final_risk_pct = self.base_risk_pct * risk_multiplier
        if rejection_reasons:
            return self._rejected_plan(
                symbol=symbol,
                direction=direction,
                equity=equity,
                entry_price=entry_price,
                stop_price=stop_price,
                risk_multiplier=risk_multiplier,
                final_risk_pct=final_risk_pct,
                rejection_reasons=rejection_reasons,
            )

        risk_amount = equity * final_risk_pct
        stop_distance = abs(entry_price - stop_price)
        stop_loss_pct = stop_distance / entry_price
        if stop_loss_pct <= 0:
            return self._rejected_plan(
                symbol=symbol,
                direction=direction,
                equity=equity,
                entry_price=entry_price,
                stop_price=stop_price,
                risk_multiplier=risk_multiplier,
                final_risk_pct=final_risk_pct,
                rejection_reasons=["ZERO_STOP_DISTANCE"],
            )

        position_notional = risk_amount / stop_loss_pct
        quantity = position_notional / entry_price
        max_loss = quantity * stop_distance

        total_open_after = current_open_risk_pct + final_risk_pct
        if total_open_after > self.max_total_open_risk_pct:
            rejection_reasons.append("EXCEEDS_MAX_TOTAL_OPEN_RISK")

        return RiskPlan(
            symbol=symbol,
            direction=direction,
            equity=float(equity),
            base_risk_pct=self.base_risk_pct,
            risk_multiplier=float(risk_multiplier),
            final_risk_pct=float(final_risk_pct),
            risk_amount=float(risk_amount),
            entry_price=float(entry_price),
            stop_price=float(stop_price),
            stop_loss_pct=float(stop_loss_pct),
            position_notional=float(position_notional),
            quantity=float(quantity),
            max_loss=float(max_loss),
            approved=not rejection_reasons,
            rejection_reasons=rejection_reasons,
            metrics_snapshot={
                "active_profile": self.cfg.risk.active_profile,
                "current_open_risk_pct": float(current_open_risk_pct),
                "total_open_after": float(total_open_after),
                "max_total_open_risk_pct": self.max_total_open_risk_pct,
            },
        )

    def compute_equity(
        self,
        wallet_balance: float,
        realized_pnl: float = 0.0,
        unrealized_pnl: float = 0.0,
    ) -> float:
        """Q28 conservative equity basis."""
        return wallet_balance + realized_pnl - max(0.0, -unrealized_pnl)

    def derive_risk_multiplier(self, consecutive_losses: int) -> float:
        cfg = self.cfg.risk.loss_streak
        if consecutive_losses >= int(cfg.reduce_after_consecutive_losses):
            return float(cfg.risk_multiplier_when_reduced)
        return 1.0

    def check_daily_loss_breach(self, daily_pnl_pct: float) -> bool:
        """Return True when daily PnL percent breaches the configured limit."""
        return daily_pnl_pct <= self.daily_loss_limit_pct

    def check_correlation_haircut_interface(
        self,
        new_symbol: str,
        new_direction: str,
        existing_symbols_directions: list[tuple[str, str]],
    ) -> dict[str, Any]:
        cfg = self.cfg.risk.correlation_haircut
        if not bool(cfg.enabled):
            return {
                "needs_haircut": False,
                "pair_in_scope": False,
                "matched_existing_symbols": [],
            }

        pair_in_scope = False
        matched_existing_symbols: list[str] = []
        for pair in list(cfg.pairs):
            if new_symbol not in pair:
                continue
            pair_in_scope = True
            paired_symbols = [symbol for symbol in pair if symbol != new_symbol]
            for symbol, direction in existing_symbols_directions:
                if symbol in paired_symbols and direction == new_direction:
                    matched_existing_symbols.append(symbol)

        return {
            "needs_haircut": bool(matched_existing_symbols),
            "pair_in_scope": pair_in_scope,
            "matched_existing_symbols": matched_existing_symbols,
        }

    def _rejected_plan(
        self,
        *,
        symbol: str,
        direction: str,
        equity: float,
        entry_price: float,
        stop_price: float,
        risk_multiplier: float,
        final_risk_pct: float,
        rejection_reasons: list[str],
    ) -> RiskPlan:
        return RiskPlan(
            symbol=symbol,
            direction=direction,
            equity=float(equity),
            base_risk_pct=self.base_risk_pct,
            risk_multiplier=float(risk_multiplier),
            final_risk_pct=float(final_risk_pct),
            risk_amount=0.0,
            entry_price=float(entry_price),
            stop_price=float(stop_price),
            stop_loss_pct=0.0,
            position_notional=0.0,
            quantity=0.0,
            max_loss=0.0,
            approved=False,
            rejection_reasons=rejection_reasons,
        )

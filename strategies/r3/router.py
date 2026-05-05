"""R3 strategy router v1.

The router only selects or rejects candidate signals. It does not place orders
or mutate position/session state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .regime import Regime, RegimeState, SessionStatus


@dataclass(frozen=True)
class PositionState:
    symbol: str
    has_position: bool
    direction: str | None = None
    strategy_name: str | None = None
    quantity: float = 0.0
    unrealized_pnl: float = 0.0
    entry_price: float | None = None


@dataclass(frozen=True)
class CooldownState:
    symbol: str
    strategy_name: str
    last_exit_reason: str | None
    last_exit_time: datetime | None
    cooldown_until: datetime | None

    def is_active(self, timestamp: datetime) -> bool:
        return (
            self.cooldown_until is not None
            and _same_time_domain(timestamp, self.cooldown_until)
            and timestamp < self.cooldown_until
        )


@dataclass(frozen=True)
class RouterDecision:
    timestamp: datetime
    symbol: str
    regime_state: RegimeState
    selected_strategy: str | None
    signal_result: Any | None
    approved: bool
    deferred: bool = False
    reason_codes: list[str] = field(default_factory=list)
    rejection_reasons: list[str] = field(default_factory=list)
    metrics_snapshot: dict[str, Any] = field(default_factory=dict)


class R3Router:
    """Initial regime-aware strategy selector for Sprint 4."""

    def __init__(self, cfg):
        self.cfg = cfg
        opp = cfg.risk.opposite_position_per_symbol
        self.allow_open_opposite = bool(opp.allow_open_opposite)
        self.use_hedge_mode = bool(opp.use_hedge_mode)
        self.wait_until_existing_closed = bool(opp.wait_until_existing_closed)

    def route(
        self,
        *,
        symbol: str,
        timestamp: datetime,
        regime_state: RegimeState,
        trend_signal_result: Any | None = None,
        mean_reversion_signal_result: Any | None = None,
        funding_reversal_signal_result: Any | None = None,
        existing_position_state: PositionState | None = None,
        cooldown_state: CooldownState | None = None,
        session_status: SessionStatus | None = None,
    ) -> RouterDecision:
        if _is_regime(regime_state, Regime.D_NO_TRADE):
            return self._reject(
                symbol,
                timestamp,
                regime_state,
                None,
                None,
                ["REJECT_REGIME_D_NO_TRADE"],
                session_status,
            )
        if _is_regime(regime_state, Regime.UNKNOWN):
            return self._reject(
                symbol,
                timestamp,
                regime_state,
                None,
                None,
                ["REJECT_REGIME_UNKNOWN"],
                session_status,
            )
        selected_strategy, candidate = self._candidate_for_regime(
            regime_state,
            trend_signal_result,
            mean_reversion_signal_result,
            funding_reversal_signal_result,
        )
        if selected_strategy is None:
            return self._reject(
                symbol,
                timestamp,
                regime_state,
                None,
                None,
                ["NO_STRATEGY_FOR_REGIME"],
                session_status,
            )
        if candidate is None or not bool(getattr(candidate, "approved", False)):
            return self._reject(
                symbol,
                timestamp,
                regime_state,
                selected_strategy,
                candidate,
                [f"NO_APPROVED_{selected_strategy.upper()}_SIGNAL"],
                session_status,
            )

        position_rejections = self._position_rejections(existing_position_state, candidate)
        if position_rejections:
            return self._reject(
                symbol,
                timestamp,
                regime_state,
                selected_strategy,
                candidate,
                position_rejections,
                session_status,
            )

        if self._cooldown_active(cooldown_state, symbol, selected_strategy, timestamp):
            return self._reject(
                symbol,
                timestamp,
                regime_state,
                selected_strategy,
                candidate,
                ["REJECT_COOLDOWN_ACTIVE"],
                session_status,
            )

        return RouterDecision(
            timestamp=timestamp,
            symbol=symbol,
            regime_state=regime_state,
            selected_strategy=selected_strategy,
            signal_result=candidate,
            approved=True,
            reason_codes=_approval_reason_codes(selected_strategy),
            metrics_snapshot=_metrics(session_status),
        )

    def _candidate_for_regime(
        self,
        regime_state: RegimeState,
        trend_signal_result: Any | None,
        mean_reversion_signal_result: Any | None,
        funding_reversal_signal_result: Any | None,
    ) -> tuple[str | None, Any | None]:
        if _is_regime(regime_state, Regime.A_TREND):
            return "trend_pullback", trend_signal_result
        if _is_regime(regime_state, Regime.B_SIDEWAYS):
            return "mean_reversion", mean_reversion_signal_result
        if _is_regime(regime_state, Regime.C_FUNDING_EXTREME):
            return "funding_reversal", funding_reversal_signal_result
        return None, None

    def _position_rejections(
        self,
        existing_position_state: PositionState | None,
        candidate: Any,
    ) -> list[str]:
        if existing_position_state is None or not existing_position_state.has_position:
            return []
        candidate_direction = getattr(candidate, "direction", None)
        if existing_position_state.direction != candidate_direction:
            rejections = ["REJECT_OPPOSITE_POSITION_EXISTS"]
            if not self.use_hedge_mode:
                rejections.append("REJECT_HEDGE_MODE_DISABLED")
            if self.wait_until_existing_closed:
                rejections.append("WAIT_FOR_EXISTING_POSITION_EXIT")
            return rejections
        return ["REJECT_POSITION_EXISTS"]

    def _cooldown_active(
        self,
        cooldown_state: CooldownState | None,
        symbol: str,
        strategy_name: str,
        timestamp: datetime,
    ) -> bool:
        return (
            cooldown_state is not None
            and cooldown_state.symbol == symbol
            and cooldown_state.strategy_name == strategy_name
            and str(cooldown_state.last_exit_reason).upper() == "SL"
            and cooldown_state.is_active(timestamp)
        )

    def _reject(
        self,
        symbol: str,
        timestamp: datetime,
        regime_state: RegimeState,
        selected_strategy: str | None,
        signal_result: Any | None,
        rejection_reasons: list[str],
        session_status: SessionStatus | None,
    ) -> RouterDecision:
        return RouterDecision(
            timestamp=timestamp,
            symbol=symbol,
            regime_state=regime_state,
            selected_strategy=selected_strategy,
            signal_result=signal_result,
            approved=False,
            rejection_reasons=rejection_reasons,
            metrics_snapshot=_metrics(session_status),
        )


def _is_regime(regime_state: RegimeState, regime: Regime) -> bool:
    return regime_state.regime == regime or str(regime_state.regime) == regime.value


def _same_time_domain(left: datetime, right: datetime) -> bool:
    return (left.tzinfo is None and right.tzinfo is None) or (
        left.tzinfo is not None and right.tzinfo is not None
    )


def _metrics(session_status: SessionStatus | None) -> dict[str, Any]:
    if session_status is None:
        return {}
    return {
        "daily_pnl_pct": session_status.daily_pnl_pct,
        "consecutive_losses": session_status.consecutive_losses,
    }


def _approval_reason_codes(selected_strategy: str) -> list[str]:
    out = [f"ROUTED_TO_{selected_strategy.upper()}"]
    if selected_strategy == "funding_reversal":
        out.append("ROUTER_ALLOW_FUNDING_REVERSAL")
    return out

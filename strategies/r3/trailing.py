"""R3 Sprint 3 initial stop and exit plan builders."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config_loader import R3Config


@dataclass(frozen=True)
class StopPlan:
    symbol: str
    direction: str
    entry_price: float
    stop_price: float
    stop_source: str
    risk_per_unit: float
    reason_codes: list[str] = field(default_factory=list)
    metrics_snapshot: dict[str, Any] = field(default_factory=dict)
    strategy_name: str = "trend_pullback"

    @property
    def initial_stop_price(self) -> float:
        return self.stop_price

    @property
    def method(self) -> str:
        return self.stop_source


@dataclass(frozen=True)
class ExitPlan:
    symbol: str
    direction: str
    entry_price: float
    stop_price: float
    risk_per_unit: float
    tp1_price: float
    tp1_fraction: float
    tp2_price: float
    tp2_fraction: float
    breakeven_trigger_r: float
    trailing_trigger_r: float
    reason_codes: list[str] = field(default_factory=list)
    metrics_snapshot: dict[str, Any] = field(default_factory=dict)
    strategy_name: str = "trend_pullback"
    target_source: str | None = None
    time_stop_hours: float | None = None


class TrendStopExitBuilder:
    """Build data-only stop and exit plans for Trend Pullback."""

    def __init__(self, cfg: R3Config):
        self.cfg = cfg
        stop_cfg = cfg.trend_pullback.stop_loss
        tp_cfg = cfg.trend_pullback.take_profit
        trailing_cfg = cfg.trailing.standard

        self.pivot_buffer_atr_mult = float(stop_cfg.pivot_buffer_atr_mult)
        self.fallback_atr_mult = float(stop_cfg.fallback_atr_mult)
        self.tp1_r = float(tp_cfg.tp1.r_multiple)
        self.tp1_fraction = float(tp_cfg.tp1.exit_pct) / 100.0
        self.tp2_r = float(tp_cfg.tp2.tp2_r)
        self.tp2_r_min = float(tp_cfg.tp2.r_multiple_min)
        self.tp2_r_max = float(tp_cfg.tp2.r_multiple_max)
        self.breakeven_trigger_r = float(trailing_cfg.move_to_breakeven_at_r)
        self.trailing_trigger_r = float(trailing_cfg.activate_trailing_at_r)

    def build_stop_plan(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        atr_1h: float,
        latest_pivot_low: float | None = None,
        latest_pivot_high: float | None = None,
    ) -> StopPlan:
        if direction not in {"long", "short"}:
            raise ValueError(f"Invalid direction: {direction}")
        if entry_price <= 0:
            raise ValueError("entry_price must be positive")
        if atr_1h <= 0:
            raise ValueError("atr_1h must be positive")

        reason_codes: list[str] = []
        if direction == "long":
            if latest_pivot_low is not None:
                stop_price = latest_pivot_low - self.pivot_buffer_atr_mult * atr_1h
                if stop_price < entry_price:
                    stop_source = "pivot_low"
                    reason_codes.append("STOP_FROM_CONFIRMED_PIVOT_LOW")
                else:
                    stop_price = entry_price - self.fallback_atr_mult * atr_1h
                    stop_source = "atr_fallback"
                    reason_codes.append("STOP_FROM_ATR_FALLBACK")
            else:
                stop_price = entry_price - self.fallback_atr_mult * atr_1h
                stop_source = "atr_fallback"
                reason_codes.append("STOP_FROM_ATR_FALLBACK")
        else:
            if latest_pivot_high is not None:
                stop_price = latest_pivot_high + self.pivot_buffer_atr_mult * atr_1h
                if stop_price > entry_price:
                    stop_source = "pivot_high"
                    reason_codes.append("STOP_FROM_CONFIRMED_PIVOT_HIGH")
                else:
                    stop_price = entry_price + self.fallback_atr_mult * atr_1h
                    stop_source = "atr_fallback"
                    reason_codes.append("STOP_FROM_ATR_FALLBACK")
            else:
                stop_price = entry_price + self.fallback_atr_mult * atr_1h
                stop_source = "atr_fallback"
                reason_codes.append("STOP_FROM_ATR_FALLBACK")

        risk_per_unit = abs(entry_price - stop_price)
        return StopPlan(
            symbol=symbol,
            direction=direction,
            entry_price=float(entry_price),
            stop_price=float(stop_price),
            stop_source=stop_source,
            risk_per_unit=float(risk_per_unit),
            reason_codes=reason_codes,
            metrics_snapshot={
                "atr_1h": float(atr_1h),
                "pivot_buffer_atr_mult": self.pivot_buffer_atr_mult,
                "fallback_atr_mult": self.fallback_atr_mult,
                "latest_pivot_low": latest_pivot_low,
                "latest_pivot_high": latest_pivot_high,
            },
        )

    def build_exit_plan(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_price: float,
    ) -> ExitPlan:
        if direction not in {"long", "short"}:
            raise ValueError(f"Invalid direction: {direction}")
        if entry_price <= 0 or stop_price <= 0:
            raise ValueError("entry_price and stop_price must be positive")

        risk_per_unit = abs(entry_price - stop_price)
        tp2_fraction = 1.0 - self.tp1_fraction

        if direction == "long":
            tp1_price = entry_price + self.tp1_r * risk_per_unit
            tp2_price = entry_price + self.tp2_r * risk_per_unit
        else:
            tp1_price = entry_price - self.tp1_r * risk_per_unit
            tp2_price = entry_price - self.tp2_r * risk_per_unit

        return ExitPlan(
            symbol=symbol,
            direction=direction,
            entry_price=float(entry_price),
            stop_price=float(stop_price),
            risk_per_unit=float(risk_per_unit),
            tp1_price=float(tp1_price),
            tp1_fraction=float(self.tp1_fraction),
            tp2_price=float(tp2_price),
            tp2_fraction=float(tp2_fraction),
            breakeven_trigger_r=self.breakeven_trigger_r,
            trailing_trigger_r=self.trailing_trigger_r,
            reason_codes=["TP1_1R", "TP2_CONFIG_R", "BREAKEVEN_AND_TRAILING_PLAN"],
            metrics_snapshot={
                "tp1_r": self.tp1_r,
                "tp2_r": self.tp2_r,
                "tp2_r_min": self.tp2_r_min,
                "tp2_r_max": self.tp2_r_max,
                "tp2_r_used": self.tp2_r,
            },
        )


class MeanReversionStopExitBuilder:
    """Build data-only stop and exit plans for Mean Reversion."""

    strategy_name = "mean_reversion"

    def __init__(self, cfg: R3Config):
        self.cfg = cfg
        exit_cfg = cfg.mean_reversion.exit
        self.stop_atr_mult = float(exit_cfg.sl_atr_mult)
        self.stop_atr_mult_min = float(exit_cfg.sl_atr_mult_min)
        self.stop_atr_mult_max = float(exit_cfg.sl_atr_mult_max)
        self.target_exit_fraction = float(exit_cfg.target_exit_pct) / 100.0
        self.secondary_exit_fraction = float(exit_cfg.secondary_exit_pct) / 100.0
        self.breakeven_trigger_r = float(exit_cfg.breakeven_trigger_r)
        self.trailing_trigger_r = float(exit_cfg.trailing_trigger_r)
        self.time_stop_hours = float(exit_cfg.time_stop_hours)

    def build_stop_plan(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        atr_1h: float,
    ) -> StopPlan:
        if direction not in {"long", "short"}:
            raise ValueError(f"Invalid direction: {direction}")
        if entry_price <= 0:
            raise ValueError("entry_price must be positive")
        if atr_1h <= 0:
            raise ValueError("atr_1h must be positive")

        if direction == "long":
            stop_price = entry_price - self.stop_atr_mult * atr_1h
        else:
            stop_price = entry_price + self.stop_atr_mult * atr_1h

        return StopPlan(
            symbol=symbol,
            direction=direction,
            entry_price=float(entry_price),
            stop_price=float(stop_price),
            stop_source="ATR_MR",
            risk_per_unit=float(abs(entry_price - stop_price)),
            reason_codes=["MR_STOP_FROM_ATR"],
            metrics_snapshot={
                "atr_1h": float(atr_1h),
                "sl_atr_mult": self.stop_atr_mult,
                "sl_atr_mult_min": self.stop_atr_mult_min,
                "sl_atr_mult_max": self.stop_atr_mult_max,
            },
            strategy_name=self.strategy_name,
        )

    def build_exit_plan(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_price: float,
        bb_middle: float | None,
        vwap: float | None,
    ) -> ExitPlan:
        if direction not in {"long", "short"}:
            raise ValueError(f"Invalid direction: {direction}")
        if entry_price <= 0 or stop_price <= 0:
            raise ValueError("entry_price and stop_price must be positive")

        target, source = self._conservative_target(direction, bb_middle, vwap)
        risk_per_unit = abs(entry_price - stop_price)
        return ExitPlan(
            symbol=symbol,
            direction=direction,
            entry_price=float(entry_price),
            stop_price=float(stop_price),
            risk_per_unit=float(risk_per_unit),
            tp1_price=float(target),
            tp1_fraction=float(self.target_exit_fraction),
            tp2_price=float(target),
            tp2_fraction=float(self.secondary_exit_fraction),
            breakeven_trigger_r=self.breakeven_trigger_r,
            trailing_trigger_r=self.trailing_trigger_r,
            reason_codes=["MR_TARGET_PLAN", "MR_TIME_STOP_PLAN"],
            metrics_snapshot={
                "bb_middle": bb_middle,
                "vwap": vwap,
                "time_stop_hours": self.time_stop_hours,
            },
            strategy_name=self.strategy_name,
            target_source=source,
            time_stop_hours=self.time_stop_hours,
        )

    def _conservative_target(
        self,
        direction: str,
        bb_middle: float | None,
        vwap: float | None,
    ) -> tuple[float, str]:
        values = []
        if bb_middle is not None:
            values.append(("BB_MIDDLE", float(bb_middle)))
        if vwap is not None:
            values.append(("VWAP", float(vwap)))
        if not values:
            raise ValueError("At least one mean reversion target is required")
        if len(values) == 1:
            return values[0][1], values[0][0]
        if direction == "long":
            return min(value for _, value in values), "CONSERVATIVE_TARGET"
        return max(value for _, value in values), "CONSERVATIVE_TARGET"


class FundingReversalStopExitBuilder:
    """Build data-only stop and exit plans for Funding Reversal."""

    strategy_name = "funding_reversal"

    def __init__(self, cfg: R3Config):
        self.cfg = cfg
        exit_cfg = cfg.funding_reversal.exit
        self.stop_atr_mult = float(exit_cfg.sl_atr_mult)
        self.stop_atr_mult_min = float(exit_cfg.sl_atr_mult_min)
        self.stop_atr_mult_max = float(exit_cfg.sl_atr_mult_max)
        self.target_exit_fraction = float(exit_cfg.target_exit_pct) / 100.0
        self.secondary_exit_fraction = float(exit_cfg.secondary_exit_pct) / 100.0
        self.breakeven_trigger_r = float(exit_cfg.breakeven_trigger_r)
        self.trailing_trigger_r = float(exit_cfg.trailing_trigger_r)
        self.time_stop_hours = float(exit_cfg.time_stop_hours)

    def build_stop_plan(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        atr_1h: float,
    ) -> StopPlan:
        if direction not in {"long", "short"}:
            raise ValueError(f"Invalid direction: {direction}")
        if entry_price <= 0:
            raise ValueError("entry_price must be positive")
        if atr_1h <= 0:
            raise ValueError("atr_1h must be positive")

        if direction == "long":
            stop_price = entry_price - self.stop_atr_mult * atr_1h
        else:
            stop_price = entry_price + self.stop_atr_mult * atr_1h

        return StopPlan(
            symbol=symbol,
            direction=direction,
            entry_price=float(entry_price),
            stop_price=float(stop_price),
            stop_source="ATR_FR",
            risk_per_unit=float(abs(entry_price - stop_price)),
            reason_codes=["FR_STOP_FROM_ATR"],
            metrics_snapshot={
                "atr_1h": float(atr_1h),
                "sl_atr_mult": self.stop_atr_mult,
                "sl_atr_mult_min": self.stop_atr_mult_min,
                "sl_atr_mult_max": self.stop_atr_mult_max,
            },
            strategy_name=self.strategy_name,
        )

    def build_exit_plan(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_price: float,
        vwap: float | None,
        mark_price: float | None = None,
    ) -> ExitPlan:
        if direction not in {"long", "short"}:
            raise ValueError(f"Invalid direction: {direction}")
        if entry_price <= 0 or stop_price <= 0:
            raise ValueError("entry_price and stop_price must be positive")

        target, source = self._target(vwap, mark_price)
        risk_per_unit = abs(entry_price - stop_price)
        return ExitPlan(
            symbol=symbol,
            direction=direction,
            entry_price=float(entry_price),
            stop_price=float(stop_price),
            risk_per_unit=float(risk_per_unit),
            tp1_price=float(target),
            tp1_fraction=float(self.target_exit_fraction),
            tp2_price=float(target),
            tp2_fraction=float(self.secondary_exit_fraction),
            breakeven_trigger_r=self.breakeven_trigger_r,
            trailing_trigger_r=self.trailing_trigger_r,
            reason_codes=["FR_TARGET_PLAN", "FR_TIME_STOP_PLAN"],
            metrics_snapshot={
                "vwap": vwap,
                "mark_price": mark_price,
                "time_stop_hours": self.time_stop_hours,
            },
            strategy_name=self.strategy_name,
            target_source=source,
            time_stop_hours=self.time_stop_hours,
        )

    def _target(self, vwap: float | None, mark_price: float | None) -> tuple[float, str]:
        if vwap is not None:
            return float(vwap), "VWAP"
        if mark_price is not None:
            return float(mark_price), "MARK_PRICE"
        raise ValueError("At least one funding reversal target is required")

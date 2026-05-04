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

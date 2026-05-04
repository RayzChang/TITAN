"""R3 Sprint 3 order-intent simulation.

This module only creates data structures. It does not call exchange APIs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .config_loader import R3Config


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    direction: str
    order_type: str
    time_in_force: str
    limit_price: float
    quantity: float
    reduce_only: bool
    reason_codes: list[str] = field(default_factory=list)
    expires_at: datetime | None = None
    signal_id: str | None = None


@dataclass(frozen=True)
class PartialFillSimulation:
    order_intent: OrderIntent
    filled_quantity: float
    remaining_quantity: float
    entry_price: float
    stop_price: float
    max_loss: float
    stop_order_intent: OrderIntent | None
    cancel_remaining_after_timeout: bool
    reason_codes: list[str] = field(default_factory=list)
    metrics_snapshot: dict[str, Any] = field(default_factory=dict)


class TrendOrderIntentBuilder:
    """Build Trend Pullback maker-limit intents."""

    def __init__(self, cfg: R3Config):
        self.cfg = cfg
        entry_cfg = cfg.trend_pullback.entry_order
        self.order_type = str(entry_cfg.order_type)
        self.time_in_force = str(entry_cfg.time_in_force)
        self.timeout_5m_bars = int(entry_cfg.timeout_5m_bars)
        self.atr_offset_mult = float(entry_cfg.atr_offset_mult)
        self.execution_timeframe = str(cfg.timeframes.execution)

    def compute_limit_price(
        self,
        *,
        direction: str,
        current_bid: float,
        current_ask: float,
        tick_size: float,
        ema20_1h: float,
        signal_5m_close: float,
        atr_5m: float,
    ) -> float:
        if direction == "long":
            return float(
                min(
                    current_bid - tick_size,
                    ema20_1h,
                    signal_5m_close - self.atr_offset_mult * atr_5m,
                )
            )
        if direction == "short":
            return float(
                max(
                    current_ask + tick_size,
                    ema20_1h,
                    signal_5m_close + self.atr_offset_mult * atr_5m,
                )
            )
        raise ValueError(f"Invalid direction: {direction}")

    def build_entry_intent(
        self,
        *,
        symbol: str,
        direction: str,
        signal_timestamp: datetime,
        current_bid: float,
        current_ask: float,
        tick_size: float,
        ema20_1h: float,
        signal_5m_close: float,
        atr_5m: float,
        quantity: float,
        signal_id: str,
    ) -> OrderIntent:
        limit_price = self.compute_limit_price(
            direction=direction,
            current_bid=current_bid,
            current_ask=current_ask,
            tick_size=tick_size,
            ema20_1h=ema20_1h,
            signal_5m_close=signal_5m_close,
            atr_5m=atr_5m,
        )
        expires_at = signal_timestamp + self.timeout_5m_bars * _timeframe_delta(
            self.execution_timeframe
        )
        return OrderIntent(
            symbol=symbol,
            direction=direction,
            order_type=self.order_type,
            time_in_force=self.time_in_force,
            limit_price=limit_price,
            quantity=float(quantity),
            reduce_only=False,
            reason_codes=["TREND_PULLBACK_MAKER_LIMIT_INTENT"],
            expires_at=expires_at,
            signal_id=signal_id,
        )


class PartialFillSimulator:
    """Simulate Q27 partial-fill bookkeeping without touching real orders."""

    def __init__(self, cfg: R3Config):
        self.cfg = cfg
        pf_cfg = cfg.trend_pullback.entry_order.partial_fill
        trigger_cfg = cfg.trailing.trigger
        self.treat_filled_as_entry = bool(pf_cfg.treat_filled_as_entry)
        self.cancel_remaining_after_timeout = bool(pf_cfg.cancel_remaining_after_timeout)
        self.stop_order_type = str(trigger_cfg.live_order_type)
        self.stop_time_in_force = str(trigger_cfg.time_in_force)
        self.stop_reduce_only = bool(trigger_cfg.reduce_only)

    def simulate(
        self,
        order_intent: OrderIntent,
        *,
        filled_quantity: float,
        entry_price: float,
        stop_price: float,
        timeout_reached: bool,
    ) -> PartialFillSimulation:
        if filled_quantity < 0:
            raise ValueError("filled_quantity must be non-negative")
        if filled_quantity > order_intent.quantity:
            raise ValueError("filled_quantity cannot exceed requested quantity")

        remaining_quantity = order_intent.quantity - filled_quantity
        max_loss = filled_quantity * abs(entry_price - stop_price)
        stop_order_intent: OrderIntent | None = None
        reason_codes: list[str] = []

        if filled_quantity > 0 and self.treat_filled_as_entry:
            stop_order_intent = OrderIntent(
                symbol=order_intent.symbol,
                direction=order_intent.direction,
                order_type=self.stop_order_type,
                time_in_force=self.stop_time_in_force,
                limit_price=float(stop_price),
                quantity=float(filled_quantity),
                reduce_only=self.stop_reduce_only,
                reason_codes=["REDUCE_ONLY_SL_FOR_FILLED_PORTION"],
                expires_at=None,
                signal_id=order_intent.signal_id,
            )
            reason_codes.append("FILLED_PORTION_TREATED_AS_ENTRY")

        cancel_remaining = (
            timeout_reached
            and remaining_quantity > 0
            and self.cancel_remaining_after_timeout
        )
        if cancel_remaining:
            reason_codes.append("UNFILLED_REMAINDER_CANCELLED_AFTER_TIMEOUT")

        return PartialFillSimulation(
            order_intent=order_intent,
            filled_quantity=float(filled_quantity),
            remaining_quantity=float(remaining_quantity),
            entry_price=float(entry_price),
            stop_price=float(stop_price),
            max_loss=float(max_loss),
            stop_order_intent=stop_order_intent,
            cancel_remaining_after_timeout=cancel_remaining,
            reason_codes=reason_codes,
            metrics_snapshot={
                "requested_quantity": float(order_intent.quantity),
                "filled_quantity": float(filled_quantity),
                "remaining_quantity": float(remaining_quantity),
            },
        )


def _timeframe_delta(timeframe: str) -> timedelta:
    unit = timeframe[-1]
    value = int(timeframe[:-1])
    if unit == "m":
        return timedelta(minutes=value)
    if unit == "h":
        return timedelta(hours=value)
    if unit == "d":
        return timedelta(days=value)
    raise ValueError(f"Unsupported timeframe: {timeframe}")

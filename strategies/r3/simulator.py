"""R3 Sprint 6 fill, exit, and cost simulation.

This module simulates historical fills and exits from existing Sprint 3-5
plans. It does not submit orders or connect to an exchange.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from .config_loader import R3Config
from .executor import OrderIntent
from .trade_log import ExitEvent, FillResult, Position


@dataclass(frozen=True)
class FundingCostResult:
    funding_cost: float
    reason_codes: list[str] = field(default_factory=list)
    events_counted: int = 0


class OrderFillSimulator:
    """Simulate maker limit order fills on completed 5m bars."""

    def __init__(self, cfg: R3Config):
        bt = cfg.backtest
        costs = bt.costs
        self.maker_fee_rate = float(costs.maker_fee_bps) / 10000.0
        self.slippage_rate = float(costs.slippage_bps) / 10000.0
        self.full_fill_ratio = float(bt.fill.full_fill_ratio)
        self.partial_fill_enabled = bool(bt.fill.partial_fill_enabled)

    def simulate_bar(
        self,
        order_intent: OrderIntent,
        bar: pd.Series,
        bar_timestamp: datetime,
        *,
        order_id: str,
    ) -> FillResult:
        if order_intent.expires_at is not None:
            if pd.Timestamp(bar_timestamp) > pd.Timestamp(order_intent.expires_at):
                return self._expired(order_intent, order_id, ["ORDER_TIMEOUT_EXPIRED"])

        limit_price = float(order_intent.limit_price)
        if order_intent.direction == "long":
            should_fill = float(bar["low"]) <= limit_price
            fill_price = limit_price * (1.0 + self.slippage_rate)
        elif order_intent.direction == "short":
            should_fill = float(bar["high"]) >= limit_price
            fill_price = limit_price * (1.0 - self.slippage_rate)
        else:
            return self._rejected(order_intent, order_id, ["INVALID_DIRECTION"])

        if not should_fill:
            return FillResult(
                order_id=order_id,
                signal_id=order_intent.signal_id,
                symbol=order_intent.symbol,
                direction=order_intent.direction,
                requested_quantity=float(order_intent.quantity),
                filled_quantity=0.0,
                fill_price=None,
                fill_timestamp=None,
                status="REJECTED",
                fee=0.0,
                slippage=0.0,
                reason_codes=["LIMIT_NOT_TOUCHED"],
            )

        fill_ratio = self.full_fill_ratio
        status = "FILLED"
        if self.partial_fill_enabled and fill_ratio < 1.0:
            status = "PARTIALLY_FILLED"
        filled_quantity = float(order_intent.quantity) * fill_ratio
        notional = abs(fill_price * filled_quantity)
        return FillResult(
            order_id=order_id,
            signal_id=order_intent.signal_id,
            symbol=order_intent.symbol,
            direction=order_intent.direction,
            requested_quantity=float(order_intent.quantity),
            filled_quantity=float(filled_quantity),
            fill_price=float(fill_price),
            fill_timestamp=bar_timestamp,
            status=status,
            fee=float(notional * self.maker_fee_rate),
            slippage=float(abs(fill_price - limit_price) * filled_quantity),
            reason_codes=["LIMIT_TOUCHED", "MAKER_FILL_SIMULATED"],
        )

    def expire_if_needed(
        self,
        order_intent: OrderIntent,
        timestamp: datetime,
        *,
        order_id: str,
    ) -> FillResult | None:
        if order_intent.expires_at is None:
            return None
        if pd.Timestamp(timestamp) <= pd.Timestamp(order_intent.expires_at):
            return None
        return self._expired(order_intent, order_id, ["ORDER_TIMEOUT_EXPIRED"])

    def _expired(
        self,
        order_intent: OrderIntent,
        order_id: str,
        reason_codes: list[str],
    ) -> FillResult:
        return FillResult(
            order_id=order_id,
            signal_id=order_intent.signal_id,
            symbol=order_intent.symbol,
            direction=order_intent.direction,
            requested_quantity=float(order_intent.quantity),
            filled_quantity=0.0,
            fill_price=None,
            fill_timestamp=None,
            status="EXPIRED",
            fee=0.0,
            slippage=0.0,
            reason_codes=reason_codes,
        )

    def _rejected(
        self,
        order_intent: OrderIntent,
        order_id: str,
        reason_codes: list[str],
    ) -> FillResult:
        return FillResult(
            order_id=order_id,
            signal_id=order_intent.signal_id,
            symbol=order_intent.symbol,
            direction=order_intent.direction,
            requested_quantity=float(order_intent.quantity),
            filled_quantity=0.0,
            fill_price=None,
            fill_timestamp=None,
            status="REJECTED",
            fee=0.0,
            slippage=0.0,
            reason_codes=reason_codes,
        )


class FundingCostCalculator:
    def __init__(self, cfg: R3Config):
        self.cfg = cfg

    def calculate(
        self,
        *,
        position: Position,
        exit_timestamp: datetime,
        funding_df: pd.DataFrame | None,
    ) -> FundingCostResult:
        if funding_df is None or funding_df.empty or "funding_rate" not in funding_df.columns:
            return FundingCostResult(0.0, ["FUNDING_DATA_MISSING"], 0)

        start = pd.Timestamp(position.entry_timestamp)
        end = pd.Timestamp(exit_timestamp)
        df = _slice_between(funding_df, start, end)
        if df.empty:
            return FundingCostResult(0.0, [], 0)

        notional = position.entry_price * position.remaining_quantity
        total = 0.0
        for rate in df["funding_rate"].dropna():
            signed = notional * float(rate)
            total += signed if position.direction == "long" else -signed
        return FundingCostResult(float(total), ["FUNDING_COST_APPLIED"], len(df))


class ExitSimulator:
    """Simulate position exits using completed 5m bars."""

    def __init__(self, cfg: R3Config):
        bt = cfg.backtest
        self.same_bar_exit_priority = str(bt.fill.same_bar_exit_priority)
        self.taker_fee_rate = float(bt.costs.taker_fee_bps) / 10000.0
        self.slippage_rate = float(bt.costs.slippage_bps) / 10000.0
        self.funding = FundingCostCalculator(cfg)

    def simulate_bar(
        self,
        position: Position,
        bar: pd.Series,
        bar_timestamp: datetime,
        *,
        funding_df: pd.DataFrame | None = None,
    ) -> list[ExitEvent]:
        if position.status != "OPEN" or position.remaining_quantity <= 0:
            return []
        if pd.Timestamp(bar_timestamp) <= pd.Timestamp(position.entry_timestamp):
            return []

        exits: list[ExitEvent] = []
        stop_hit = self._stop_hit(position, bar)
        tp1_hit = self._tp_hit(position, bar, position.tp1_price)
        tp2_hit = self._tp_hit(position, bar, position.tp2_price)

        if stop_hit and (tp1_hit or tp2_hit) and self.same_bar_exit_priority == "conservative_stop_first":
            exits.append(self._exit(position, "STOP_LOSS", position.stop_price, bar_timestamp, position.remaining_quantity, funding_df))
            return exits
        if stop_hit:
            exits.append(self._exit(position, "STOP_LOSS", position.stop_price, bar_timestamp, position.remaining_quantity, funding_df))
            return exits

        if tp1_hit and not position.tp1_done and position.tp1_price is not None:
            qty = min(position.remaining_quantity, position.quantity * float(_safe(position.trailing_state.get("tp1_fraction"), 0.5)))
            exits.append(self._exit(position, "TAKE_PROFIT_1", position.tp1_price, bar_timestamp, qty, funding_df))
            position.tp1_done = True
            position.remaining_quantity -= qty
            position.realized_pnl += exits[-1].realized_pnl
            position.fees_paid += exits[-1].fee
            position.funding_paid += exits[-1].funding_cost

        if position.remaining_quantity <= 0:
            position.status = "CLOSED"
            return exits

        if tp2_hit and position.tp2_price is not None:
            exits.append(self._exit(position, "TAKE_PROFIT_2", position.tp2_price, bar_timestamp, position.remaining_quantity, funding_df))
            return exits

        if position.time_stop_at is not None and pd.Timestamp(bar_timestamp) >= pd.Timestamp(position.time_stop_at):
            exits.append(self._exit(position, "TIME_STOP", float(bar["close"]), bar_timestamp, position.remaining_quantity, funding_df))
            return exits

        return exits

    def _stop_hit(self, position: Position, bar: pd.Series) -> bool:
        if position.direction == "long":
            return float(bar["low"]) <= position.stop_price
        return float(bar["high"]) >= position.stop_price

    def _tp_hit(self, position: Position, bar: pd.Series, price: float | None) -> bool:
        if price is None:
            return False
        if position.direction == "long":
            return float(bar["high"]) >= price
        return float(bar["low"]) <= price

    def _exit(
        self,
        position: Position,
        exit_type: str,
        raw_exit_price: float,
        exit_timestamp: datetime,
        quantity: float,
        funding_df: pd.DataFrame | None,
    ) -> ExitEvent:
        if position.direction == "long":
            exit_price = raw_exit_price * (1.0 - self.slippage_rate)
            realized = (exit_price - position.entry_price) * quantity
        else:
            exit_price = raw_exit_price * (1.0 + self.slippage_rate)
            realized = (position.entry_price - exit_price) * quantity

        fee = abs(exit_price * quantity) * self.taker_fee_rate
        slippage = abs(exit_price - raw_exit_price) * quantity
        funding = self.funding.calculate(
            position=position,
            exit_timestamp=exit_timestamp,
            funding_df=funding_df,
        )
        event = ExitEvent(
            position_id=position.position_id,
            symbol=position.symbol,
            strategy_name=position.strategy_name,
            direction=position.direction,
            exit_type=exit_type,
            exit_price=float(exit_price),
            exit_timestamp=exit_timestamp,
            quantity=float(quantity),
            realized_pnl=float(realized),
            fee=float(fee),
            slippage=float(slippage),
            funding_cost=float(funding.funding_cost),
            reason_codes=[exit_type, *funding.reason_codes],
        )
        if quantity >= position.remaining_quantity:
            position.remaining_quantity = 0.0
            position.status = "CLOSED"
        return event


def _slice_between(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    if not isinstance(df.index, pd.DatetimeIndex):
        return df.iloc[0:0]
    left = start
    right = end
    if df.index.tz is not None and left.tzinfo is None:
        left = left.tz_localize(df.index.tz)
    elif df.index.tz is None and left.tzinfo is not None:
        left = left.tz_convert(None)
    if df.index.tz is not None and right.tzinfo is None:
        right = right.tz_localize(df.index.tz)
    elif df.index.tz is None and right.tzinfo is not None:
        right = right.tz_convert(None)
    return df.loc[(df.index > left) & (df.index <= right)]


def _safe(value: Any, default: float) -> float:
    if value is None:
        return default
    return float(value)

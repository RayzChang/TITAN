"""R3 Sprint 6 backtest engine.

The engine runs historical simulation only. It does not perform L0-L6 research
checks, dry-run execution, or real exchange order submission.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from .config_loader import R3Config
from .executor import OrderIntent
from .funding_reversal import FundingReversalStrategy
from .indicators import (
    attach_core_indicators,
    funding_z as funding_z_score,
    premium_z as premium_z_score,
)
from .mean_reversion import MeanReversionStrategy
from .performance import (
    calculate_metrics,
    daily_pnl_from_equity,
    drawdown_curve,
    equity_curve_from_points,
)
from .regime import (
    Regime,
    RegimeClassifier,
    RegimeState,
    build_snapshot_from_indicators,
)
from .router import CooldownState, PositionState, R3Router
from .simulator import ExitSimulator, OrderFillSimulator
from .trade_log import (
    FillResult,
    PortfolioState,
    Position,
    exit_events_to_frame,
    write_report_files,
)
from .trend_pullback import TrendPullbackStrategy


@dataclass
class PendingOrder:
    order_id: str
    signal_result: Any
    order_intent: OrderIntent
    created_at: datetime


@dataclass
class BacktestResult:
    portfolio: PortfolioState
    trade_log: pd.DataFrame
    daily_pnl: pd.DataFrame
    equity_curve: pd.DataFrame
    drawdown_curve: pd.DataFrame
    metrics: dict[str, Any]
    fills: list[FillResult] = field(default_factory=list)
    output_dir: Path | None = None
    report_paths: dict[str, Path] = field(default_factory=dict)
    data_warnings: list[str] = field(default_factory=list)


class BacktestEngine:
    """Bar-by-bar R3 strategy simulation for Sprint 6."""

    def __init__(
        self,
        cfg: R3Config,
        *,
        initial_capital: float | None = None,
    ):
        self.cfg = cfg
        self.initial_capital = float(
            initial_capital if initial_capital is not None else cfg.backtest.initial_capital
        )
        self.regime_classifier = RegimeClassifier(cfg)
        self.router = R3Router(cfg)
        self.trend = TrendPullbackStrategy(cfg)
        self.mean_reversion = MeanReversionStrategy(cfg)
        self.funding_reversal = FundingReversalStrategy(cfg)
        self.fill_simulator = OrderFillSimulator(cfg)
        self.exit_simulator = ExitSimulator(cfg)
        self.tick_size_default = float(cfg.backtest.execution.tick_size_default)
        self.spread_bps = float(cfg.backtest.execution.synthetic_spread_bps)

    def run(
        self,
        *,
        data_by_symbol: dict[str, dict[str, pd.DataFrame]],
        funding_by_symbol: dict[str, pd.DataFrame] | None = None,
        premium_by_symbol: dict[str, pd.DataFrame] | None = None,
        output_dir: str | Path | None = None,
    ) -> BacktestResult:
        funding_by_symbol = funding_by_symbol or {}
        premium_by_symbol = premium_by_symbol or {}
        prepared = {
            symbol: self._prepare_symbol_data(frames)
            for symbol, frames in data_by_symbol.items()
        }
        funding_z_by_symbol = {
            symbol: self._prepare_funding_z(funding_by_symbol.get(symbol))
            for symbol in data_by_symbol
        }
        premium_z_by_symbol = {
            symbol: self._prepare_premium_z(premium_by_symbol.get(symbol))
            for symbol in data_by_symbol
        }

        portfolio = PortfolioState(
            initial_capital=self.initial_capital,
            current_equity=self.initial_capital,
            cash_balance=self.initial_capital,
        )
        pending_orders: dict[str, PendingOrder] = {}
        cooldowns: dict[tuple[str, str], CooldownState] = {}
        fills: list[FillResult] = []
        equity_points: list[dict[str, Any]] = []
        data_warnings: list[str] = []

        timeline = self._combined_5m_timeline(prepared)
        for ts in timeline:
            for symbol, frames in prepared.items():
                df_5m = frames["5m"]
                if ts not in df_5m.index:
                    continue
                bar = df_5m.loc[ts]
                self._process_exits(
                    portfolio,
                    symbol,
                    bar,
                    ts,
                    funding_by_symbol.get(symbol),
                    cooldowns,
                )
                self._process_pending_order(
                    portfolio,
                    pending_orders,
                    fills,
                    symbol,
                    bar,
                    ts,
                )
                if symbol in portfolio.open_positions or symbol in pending_orders:
                    continue
                self._evaluate_new_order(
                    portfolio,
                    pending_orders,
                    cooldowns,
                    symbol,
                    frames,
                    ts,
                    funding_z_by_symbol.get(symbol),
                    premium_z_by_symbol.get(symbol),
                    data_warnings,
                )
            self._mark_equity(portfolio, prepared, ts, equity_points)

        self._close_remaining_positions(portfolio, prepared, funding_by_symbol)
        if equity_points:
            equity_points.append({
                "timestamp": equity_points[-1]["timestamp"],
                "equity": float(portfolio.current_equity),
            })
        equity_curve = equity_curve_from_points(equity_points)
        if equity_curve.empty:
            equity_curve = pd.DataFrame([{
                "timestamp": pd.Timestamp.utcnow().to_pydatetime(),
                "equity": portfolio.current_equity,
            }])
        daily_pnl = daily_pnl_from_equity(equity_curve)
        drawdown = drawdown_curve(equity_curve)
        trade_log = self._trade_log_frame(portfolio)
        data_warnings.extend(self._data_warnings_from_trade_log(trade_log))
        unique_data_warnings = sorted(set(data_warnings))
        metrics = calculate_metrics(
            cfg=self.cfg,
            initial_capital=self.initial_capital,
            equity_curve=equity_curve,
            trade_log=trade_log,
            daily_pnl=daily_pnl,
            total_fees=portfolio.total_fees,
            total_slippage=portfolio.total_slippage,
            total_funding=portfolio.total_funding,
        )
        if unique_data_warnings:
            metrics["data_warnings"] = unique_data_warnings

        report_paths: dict[str, Path] = {}
        out_path = Path(output_dir) if output_dir is not None else None
        if out_path is not None:
            report_paths = write_report_files(
                output_dir=out_path,
                trade_log=trade_log,
                daily_pnl=daily_pnl,
                equity_curve=equity_curve,
                drawdown_curve=drawdown,
                metrics=metrics,
            )

        return BacktestResult(
            portfolio=portfolio,
            trade_log=trade_log,
            daily_pnl=daily_pnl,
            equity_curve=equity_curve,
            drawdown_curve=drawdown,
            metrics=metrics,
            fills=fills,
            output_dir=out_path,
            report_paths=report_paths,
            data_warnings=unique_data_warnings,
        )

    def _prepare_symbol_data(self, frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        for timeframe in ["5m", "1h", "4h"]:
            if timeframe not in frames:
                raise ValueError(f"Missing {timeframe} data")
            completed = completed_bars(frames[timeframe], timeframe, self.cfg)
            out[timeframe] = attach_core_indicators(completed, self.cfg, timeframe)
        return out

    def _prepare_funding_z(self, funding_df: pd.DataFrame | None) -> pd.Series:
        if funding_df is None or funding_df.empty or "funding_rate" not in funding_df.columns:
            return pd.Series(dtype=float)
        fcfg = self.cfg.funding
        return funding_z_score(
            funding_df["funding_rate"],
            int(fcfg.lookback_days),
            int(fcfg.default_interval_hours),
            int(fcfg.min_samples_required),
        ).dropna()

    def _prepare_premium_z(self, premium_df: pd.DataFrame | None) -> pd.Series:
        if premium_df is None or premium_df.empty or "close" not in premium_df.columns:
            return pd.Series(dtype=float)
        fcfg = self.cfg.funding
        window = int(fcfg.lookback_days * 24)
        completed = completed_bars(premium_df, "1h", self.cfg)
        return premium_z_score(
            completed["close"],
            window=window,
            min_samples=int(fcfg.min_samples_required),
        ).dropna()

    def _combined_5m_timeline(self, prepared: dict[str, dict[str, pd.DataFrame]]) -> list[datetime]:
        values: set[pd.Timestamp] = set()
        for frames in prepared.values():
            values.update(pd.Timestamp(v) for v in frames["5m"].index)
        return [v.to_pydatetime() for v in sorted(values)]

    def _process_exits(
        self,
        portfolio: PortfolioState,
        symbol: str,
        bar: pd.Series,
        timestamp: datetime,
        funding_df: pd.DataFrame | None,
        cooldowns: dict[tuple[str, str], CooldownState],
    ) -> None:
        position = portfolio.open_positions.get(symbol)
        if position is None:
            return
        events = self.exit_simulator.simulate_bar(
            position,
            bar,
            timestamp,
            funding_df=funding_df,
        )
        for event in events:
            portfolio.closed_trades.append(event)
            portfolio.cash_balance += event.realized_pnl - event.fee - event.funding_cost
            portfolio.total_fees += event.fee
            portfolio.total_slippage += event.slippage
            portfolio.total_funding += event.funding_cost
            if event.exit_type == "STOP_LOSS":
                cooldown = self._cooldown_after_stop(position, timestamp)
                if cooldown is not None:
                    cooldowns[(symbol, position.strategy_name)] = cooldown
        if position.status == "CLOSED":
            portfolio.open_positions.pop(symbol, None)

    def _process_pending_order(
        self,
        portfolio: PortfolioState,
        pending_orders: dict[str, PendingOrder],
        fills: list[FillResult],
        symbol: str,
        bar: pd.Series,
        timestamp: datetime,
    ) -> None:
        pending = pending_orders.get(symbol)
        if pending is None:
            return
        expired = self.fill_simulator.expire_if_needed(
            pending.order_intent,
            timestamp,
            order_id=pending.order_id,
        )
        if expired is not None:
            fills.append(expired)
            pending_orders.pop(symbol, None)
            return
        fill = self.fill_simulator.simulate_bar(
            pending.order_intent,
            bar,
            timestamp,
            order_id=pending.order_id,
        )
        if fill.status not in {"FILLED", "PARTIALLY_FILLED"}:
            return
        fills.append(fill)
        pending_orders.pop(symbol, None)
        position = self._position_from_fill(fill, pending.signal_result)
        portfolio.open_positions[symbol] = position
        portfolio.cash_balance -= fill.fee
        portfolio.total_fees += fill.fee
        portfolio.total_slippage += fill.slippage

    def _evaluate_new_order(
        self,
        portfolio: PortfolioState,
        pending_orders: dict[str, PendingOrder],
        cooldowns: dict[tuple[str, str], CooldownState],
        symbol: str,
        frames: dict[str, pd.DataFrame],
        timestamp: datetime,
        funding_z_series: pd.Series | None,
        premium_z_series: pd.Series | None,
        data_warnings: list[str],
    ) -> None:
        df_5m = frames["5m"].loc[:timestamp]
        df_1h = frames["1h"].loc[:timestamp]
        df_4h = frames["4h"].loc[:timestamp]
        if df_5m.empty or df_1h.empty or df_4h.empty:
            return
        regime_state = self._classify_regime(
            symbol,
            timestamp,
            df_1h,
            df_4h,
            funding_z_series,
            premium_z_series,
        )
        current_close = float(df_5m.iloc[-1]["close"])
        current_bid, current_ask = self._bid_ask(current_close)
        signal = self._evaluate_strategy(
            symbol=symbol,
            timestamp=timestamp,
            regime_state=regime_state,
            df_1h=df_1h,
            df_5m=df_5m,
            equity=portfolio.current_equity,
            current_bid=current_bid,
            current_ask=current_ask,
        )
        decision = self.router.route(
            symbol=symbol,
            timestamp=timestamp,
            regime_state=regime_state,
            trend_signal_result=signal if getattr(signal, "strategy_name", None) == "trend_pullback" else None,
            mean_reversion_signal_result=signal if getattr(signal, "strategy_name", None) == "mean_reversion" else None,
            funding_reversal_signal_result=signal if getattr(signal, "strategy_name", None) == "funding_reversal" else None,
            existing_position_state=None,
            cooldown_state=cooldowns.get((symbol, getattr(signal, "strategy_name", ""))),
        )
        if not decision.approved or signal is None:
            return
        intent = getattr(signal, "entry_order_intent", None)
        if intent is None:
            data_warnings.append("APPROVED_SIGNAL_WITHOUT_ORDER_INTENT")
            return
        order_id = f"order:{intent.signal_id}"
        pending_orders[symbol] = PendingOrder(order_id, signal, intent, timestamp)

    def _classify_regime(
        self,
        symbol: str,
        timestamp: datetime,
        df_1h: pd.DataFrame,
        df_4h: pd.DataFrame,
        funding_z_series: pd.Series | None,
        premium_z_series: pd.Series | None,
    ) -> RegimeState:
        funding_value = _latest_value(funding_z_series, timestamp)
        premium_value = _latest_value(premium_z_series, timestamp)
        snap = build_snapshot_from_indicators(
            cfg=self.cfg,
            df_4h_with_indicators=df_4h,
            df_1h_with_indicators=df_1h,
            funding_z_value=funding_value,
            premium_z_value=premium_value,
            extreme_vol_at_t=False,
            consecutive_large_candles_at_t=False,
            bar_index_1h=len(df_1h) - 1,
            bars_per_day_1h=int(self.cfg.backtest.data.bars_per_day_1h),
        )
        return self.regime_classifier.classify(timestamp, symbol, snap)

    def _evaluate_strategy(
        self,
        *,
        symbol: str,
        timestamp: datetime,
        regime_state: RegimeState,
        df_1h: pd.DataFrame,
        df_5m: pd.DataFrame,
        equity: float,
        current_bid: float,
        current_ask: float,
    ) -> Any | None:
        kwargs = {
            "symbol": symbol,
            "as_of": timestamp,
            "regime_state": regime_state,
            "df_1h": df_1h,
            "df_5m": df_5m,
            "equity": equity,
            "current_bid": current_bid,
            "current_ask": current_ask,
            "tick_size": self.tick_size_default,
        }
        if regime_state.regime == Regime.A_TREND:
            return self.trend.evaluate(**kwargs)
        if regime_state.regime == Regime.B_SIDEWAYS:
            return self.mean_reversion.evaluate(**kwargs)
        if regime_state.regime == Regime.C_FUNDING_EXTREME:
            return self.funding_reversal.evaluate(**kwargs)
        return None

    def _position_from_fill(self, fill: FillResult, signal: Any) -> Position:
        exit_plan = signal.exit_plan
        stop_plan = signal.stop_plan
        time_stop_at = None
        if getattr(exit_plan, "time_stop_hours", None) is not None and fill.fill_timestamp is not None:
            time_stop_at = fill.fill_timestamp + timedelta(hours=float(exit_plan.time_stop_hours))
        return Position(
            position_id=f"pos:{fill.order_id}",
            signal_id=fill.signal_id,
            symbol=fill.symbol,
            strategy_name=signal.strategy_name,
            direction=fill.direction,
            entry_timestamp=fill.fill_timestamp,
            entry_price=float(fill.fill_price),
            quantity=float(fill.filled_quantity),
            remaining_quantity=float(fill.filled_quantity),
            stop_price=float(stop_plan.stop_price),
            tp1_price=float(exit_plan.tp1_price) if exit_plan else None,
            tp2_price=float(exit_plan.tp2_price) if exit_plan else None,
            time_stop_at=time_stop_at,
            trailing_state={
                "tp1_fraction": float(exit_plan.tp1_fraction) if exit_plan else 1.0,
                "trailing_trigger_r": float(exit_plan.trailing_trigger_r) if exit_plan else 0.0,
                "risk_per_unit": float(exit_plan.risk_per_unit) if exit_plan else 0.0,
            },
            fees_paid=float(fill.fee),
            entry_fee=float(fill.fee),
            entry_slippage=float(fill.slippage),
        )

    def _mark_equity(
        self,
        portfolio: PortfolioState,
        prepared: dict[str, dict[str, pd.DataFrame]],
        timestamp: datetime,
        equity_points: list[dict[str, Any]],
    ) -> None:
        unrealized = 0.0
        for symbol, position in portfolio.open_positions.items():
            df_5m = prepared[symbol]["5m"]
            if timestamp not in df_5m.index:
                continue
            close = float(df_5m.loc[timestamp, "close"])
            if position.direction == "long":
                position.unrealized_pnl = (close - position.entry_price) * position.remaining_quantity
            else:
                position.unrealized_pnl = (position.entry_price - close) * position.remaining_quantity
            unrealized += position.unrealized_pnl
        portfolio.current_equity = portfolio.cash_balance + unrealized
        equity_points.append({"timestamp": timestamp, "equity": float(portfolio.current_equity)})

    def _close_remaining_positions(
        self,
        portfolio: PortfolioState,
        prepared: dict[str, dict[str, pd.DataFrame]],
        funding_by_symbol: dict[str, pd.DataFrame] | None,
    ) -> None:
        for symbol, position in list(portfolio.open_positions.items()):
            df_5m = prepared[symbol]["5m"]
            if df_5m.empty:
                continue
            last_ts = df_5m.index[-1].to_pydatetime()
            last_bar = df_5m.iloc[-1]
            event = self.exit_simulator._exit(
                position,
                "MANUAL_SIMULATION_CLOSE",
                float(last_bar["close"]),
                last_ts,
                position.remaining_quantity,
                (funding_by_symbol or {}).get(symbol),
            )
            portfolio.closed_trades.append(event)
            portfolio.cash_balance += event.realized_pnl - event.fee - event.funding_cost
            portfolio.total_fees += event.fee
            portfolio.total_slippage += event.slippage
            portfolio.total_funding += event.funding_cost
            portfolio.open_positions.pop(symbol, None)
        portfolio.current_equity = portfolio.cash_balance

    def _cooldown_after_stop(self, position: Position, timestamp: datetime) -> CooldownState | None:
        strategy_cfg = getattr(self.cfg, position.strategy_name)
        cooldown_cfg = getattr(strategy_cfg, "cooldown_after", None)
        if cooldown_cfg is None:
            return None
        bars = int(cooldown_cfg.sl_exit_1h_bars)
        if bars <= 0:
            return None
        return CooldownState(
            symbol=position.symbol,
            strategy_name=position.strategy_name,
            last_exit_reason="SL",
            last_exit_time=timestamp,
            cooldown_until=timestamp + timedelta(hours=bars),
        )

    def _trade_log_frame(self, portfolio: PortfolioState) -> pd.DataFrame:
        df = exit_events_to_frame(portfolio.closed_trades)
        if df.empty:
            return pd.DataFrame(columns=[
                "position_id", "symbol", "strategy_name", "direction", "exit_type",
                "exit_price", "exit_timestamp", "quantity", "realized_pnl", "fee",
                "slippage", "funding_cost", "reason_codes", "risk_per_unit",
            ])
        df["risk_per_unit"] = None
        return df

    def _data_warnings_from_trade_log(self, trade_log: pd.DataFrame) -> list[str]:
        if trade_log.empty or "reason_codes" not in trade_log.columns:
            return []
        warnings: list[str] = []
        for codes in trade_log["reason_codes"]:
            if isinstance(codes, list) and "FUNDING_DATA_MISSING" in codes:
                warnings.append("FUNDING_DATA_MISSING")
            elif isinstance(codes, str) and "FUNDING_DATA_MISSING" in codes:
                warnings.append("FUNDING_DATA_MISSING")
        return warnings

    def _bid_ask(self, close: float) -> tuple[float, float]:
        half_spread = close * (self.spread_bps / 10000.0) / 2.0
        return close - half_spread, close + half_spread


def completed_bars(df: pd.DataFrame, timeframe: str, cfg: R3Config) -> pd.DataFrame:
    """Return completed bars with close-time index for conservative alignment."""
    if df.empty:
        return df.copy()
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("Backtest data requires DatetimeIndex")
    out = df.sort_index().copy()
    index_mode = str(cfg.backtest.data.input_index)
    if index_mode == "open_time":
        out.index = out.index + _timeframe_delta(timeframe)
    elif index_mode != "close_time":
        raise ValueError(f"Unsupported input_index: {index_mode}")
    return out


def completed_slice(df: pd.DataFrame, as_of: datetime, timeframe: str, cfg: R3Config) -> pd.DataFrame:
    aligned = completed_bars(df, timeframe, cfg)
    ts = _coerce_timestamp_for_index(aligned.index, as_of)
    return aligned.loc[aligned.index <= ts]


def _latest_value(series: pd.Series | None, timestamp: datetime) -> float | None:
    if series is None or series.empty:
        return None
    ts = _coerce_timestamp_for_index(series.index, timestamp)
    pos = series.index.searchsorted(ts, side="right") - 1
    if pos < 0:
        return None
    value = series.iloc[pos]
    if pd.isna(value):
        return None
    return float(value)


def _coerce_timestamp_for_index(index: pd.DatetimeIndex, timestamp: datetime) -> pd.Timestamp:
    ts = pd.Timestamp(timestamp)
    if index.tz is not None and ts.tzinfo is None:
        return ts.tz_localize(index.tz)
    if index.tz is None and ts.tzinfo is not None:
        return ts.tz_convert(None)
    return ts


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

"""Target-specific metric helpers for Sprint 7 validation."""
from __future__ import annotations

from typing import Any

import pandas as pd

from ..performance import calculate_metrics, daily_pnl_from_equity
from .common import SINGLE_STRATEGY_TARGETS, TARGET_FULL_PORTFOLIO


def target_artifacts(context: Any, target: str, backtest_result: Any):
    """Return trade log, daily PnL, equity curve, and metrics for one target."""
    if target == TARGET_FULL_PORTFOLIO:
        trade_log = backtest_result.trade_log.copy()
        daily_pnl = backtest_result.daily_pnl.copy()
        equity_curve = backtest_result.equity_curve.copy()
    else:
        strategy_name = SINGLE_STRATEGY_TARGETS[target]
        trade_log = filter_trade_log_for_target(backtest_result.trade_log, strategy_name)
        daily_pnl, equity_curve = build_target_specific_series(
            trade_log,
            context.initial_capital,
            source_equity_curve=backtest_result.equity_curve,
        )

    totals = cost_totals(trade_log)
    metrics = calculate_metrics(
        cfg=context.cfg,
        initial_capital=context.initial_capital,
        equity_curve=equity_curve,
        trade_log=trade_log,
        daily_pnl=daily_pnl,
        total_fees=totals["fees"],
        total_slippage=totals["slippage"],
        total_funding=totals["funding"],
    )
    metrics["validation_target"] = target
    metrics["validation_type"] = (
        "full_portfolio_validation"
        if target == TARGET_FULL_PORTFOLIO
        else "single_strategy_diagnostic"
    )
    if getattr(backtest_result, "data_warnings", None):
        metrics["data_warnings"] = list(backtest_result.data_warnings)
    return trade_log, daily_pnl, equity_curve, metrics


def filter_trade_log_for_target(trade_log: pd.DataFrame, strategy_name: str) -> pd.DataFrame:
    if trade_log.empty:
        return trade_log.copy()
    if "strategy_name" not in trade_log:
        return trade_log.iloc[0:0].copy()
    return trade_log[trade_log["strategy_name"] == strategy_name].copy()


def build_target_specific_series(
    trade_log: pd.DataFrame,
    initial_capital: float,
    *,
    source_equity_curve: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if trade_log.empty:
        timestamp = _fallback_timestamp(source_equity_curve)
        equity_curve = pd.DataFrame([{
            "timestamp": timestamp,
            "equity": float(initial_capital),
        }])
        return daily_pnl_from_equity(equity_curve), equity_curve

    timestamp_col = "exit_timestamp" if "exit_timestamp" in trade_log else "timestamp"
    ordered = trade_log.copy()
    ordered[timestamp_col] = pd.to_datetime(ordered[timestamp_col], utc=True)
    ordered = ordered.sort_values(timestamp_col)
    equity = float(initial_capital)
    points = []
    first_ts = ordered[timestamp_col].iloc[0]
    points.append({
        "timestamp": first_ts - pd.Timedelta(microseconds=1),
        "equity": equity,
    })
    for _, row in ordered.iterrows():
        equity += _net_trade_change(row)
        points.append({
            "timestamp": row[timestamp_col],
            "equity": float(equity),
        })
    equity_curve = pd.DataFrame(points)
    return daily_pnl_from_equity(equity_curve), equity_curve


def cost_totals(trade_log: pd.DataFrame) -> dict[str, float]:
    if trade_log.empty:
        return {"fees": 0.0, "slippage": 0.0, "funding": 0.0}
    return {
        "fees": _sum_column(trade_log, "fee"),
        "slippage": _sum_column(trade_log, "slippage"),
        "funding": _sum_column(trade_log, "funding_cost"),
    }


def _net_trade_change(row: pd.Series) -> float:
    return (
        float(row.get("realized_pnl", 0.0) or 0.0)
        - float(row.get("fee", 0.0) or 0.0)
        - float(row.get("funding_cost", 0.0) or 0.0)
    )


def _sum_column(frame: pd.DataFrame, column: str) -> float:
    if column not in frame:
        return 0.0
    return float(pd.to_numeric(frame[column], errors="coerce").fillna(0.0).sum())


def _fallback_timestamp(source_equity_curve: pd.DataFrame | None):
    if source_equity_curve is not None and not source_equity_curve.empty:
        return source_equity_curve["timestamp"].iloc[0]
    return pd.Timestamp.utcnow()

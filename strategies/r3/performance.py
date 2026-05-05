"""R3 Sprint 6 backtest performance metrics."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .config_loader import R3Config


def equity_curve_from_points(points: list[dict[str, Any]]) -> pd.DataFrame:
    if not points:
        return pd.DataFrame(columns=["timestamp", "equity"])
    df = pd.DataFrame(points).sort_values("timestamp")
    return df.reset_index(drop=True)


def drawdown_curve(equity_curve: pd.DataFrame) -> pd.DataFrame:
    if equity_curve.empty:
        return pd.DataFrame(columns=["timestamp", "equity", "drawdown", "drawdown_pct"])
    out = equity_curve.copy()
    running_max = out["equity"].cummax()
    out["drawdown"] = out["equity"] - running_max
    out["drawdown_pct"] = out["drawdown"] / running_max.replace(0.0, np.nan) * 100.0
    return out[["timestamp", "equity", "drawdown", "drawdown_pct"]]


def daily_pnl_from_equity(equity_curve: pd.DataFrame) -> pd.DataFrame:
    if equity_curve.empty:
        return pd.DataFrame(columns=["date", "daily_pnl"])
    out = equity_curve.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
    out["date"] = out["timestamp"].dt.date
    daily = out.groupby("date")["equity"].last().diff().fillna(0.0)
    return daily.reset_index(name="daily_pnl")


def calculate_metrics(
    *,
    cfg: R3Config,
    initial_capital: float,
    equity_curve: pd.DataFrame,
    trade_log: pd.DataFrame,
    daily_pnl: pd.DataFrame,
    total_fees: float,
    total_slippage: float,
    total_funding: float,
) -> dict[str, Any]:
    annualization_days = float(cfg.backtest.metrics.annualization_days)
    final_equity = float(equity_curve["equity"].iloc[-1]) if not equity_curve.empty else initial_capital
    net_profit = final_equity - initial_capital
    total_return_pct = net_profit / initial_capital * 100.0 if initial_capital else 0.0

    dd = drawdown_curve(equity_curve)
    max_drawdown_pct = float(abs(dd["drawdown_pct"].min())) if not dd.empty else 0.0

    total_trades = int(len(trade_log))
    pnl = trade_log["realized_pnl"] if "realized_pnl" in trade_log.columns and total_trades else pd.Series(dtype=float)
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    gross_profit = float(wins.sum()) if not wins.empty else 0.0
    gross_loss = abs(float(losses.sum())) if not losses.empty else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    average_trade_pnl = float(pnl.mean()) if total_trades else 0.0

    if "risk_per_unit" in trade_log.columns and "quantity" in trade_log.columns and total_trades:
        risk_per_unit = pd.to_numeric(trade_log["risk_per_unit"], errors="coerce").abs()
        quantity = pd.to_numeric(trade_log["quantity"], errors="coerce").abs()
        risk_amount = (risk_per_unit * quantity).replace(0.0, np.nan)
        realized_pnl = pd.to_numeric(trade_log["realized_pnl"], errors="coerce")
        average_trade_r_value = (realized_pnl / risk_amount).mean()
        average_trade_r = 0.0 if pd.isna(average_trade_r_value) else float(average_trade_r_value)
    else:
        average_trade_r = 0.0

    daily_values = daily_pnl["daily_pnl"] if "daily_pnl" in daily_pnl.columns and not daily_pnl.empty else pd.Series(dtype=float)
    average_daily_pnl = float(daily_values.mean()) if not daily_values.empty else 0.0
    median_daily_pnl = float(daily_values.median()) if not daily_values.empty else 0.0
    best_day = float(daily_values.max()) if not daily_values.empty else 0.0
    worst_day = float(daily_values.min()) if not daily_values.empty else 0.0
    daily_returns = daily_values / initial_capital if initial_capital else pd.Series(dtype=float)
    sharpe_ratio = _sharpe(daily_returns, annualization_days)
    calmar_ratio = (total_return_pct / 100.0) / (max_drawdown_pct / 100.0) if max_drawdown_pct > 0 else 0.0

    return {
        "initial_capital": float(initial_capital),
        "final_equity": final_equity,
        "net_profit": float(net_profit),
        "total_return_pct": float(total_return_pct),
        "max_drawdown_pct": max_drawdown_pct,
        "total_trades": total_trades,
        "win_rate": float(len(wins) / total_trades) if total_trades else 0.0,
        "profit_factor": float(profit_factor),
        "average_trade_pnl": average_trade_pnl,
        "average_trade_r": average_trade_r,
        "average_daily_pnl": average_daily_pnl,
        "median_daily_pnl": median_daily_pnl,
        "best_day": best_day,
        "worst_day": worst_day,
        "total_fees": float(total_fees),
        "total_slippage": float(total_slippage),
        "total_funding": float(total_funding),
        "sharpe_ratio": float(sharpe_ratio),
        "calmar_ratio": float(calmar_ratio),
    }


def _sharpe(daily_returns: pd.Series, annualization_days: float) -> float:
    if daily_returns.empty:
        return 0.0
    std = float(daily_returns.std(ddof=0))
    if std == 0.0 or np.isnan(std):
        return 0.0
    return float(daily_returns.mean() / std * np.sqrt(annualization_days))

"""L0 pure backtest validation."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from ..backtest_engine import BacktestEngine, BacktestResult
from .common import (
    LevelResult,
    ValidationContext,
    result_artifacts,
    status_from_checks,
    write_csv,
    write_json,
    write_markdown,
)
from .target_metrics import target_artifacts


def run_l0(context: ValidationContext, target: str) -> LevelResult:
    out_dir = context.child_output_dir(target, "L0")
    backtest = _get_or_run_backtest(context, target)
    if backtest is None:
        return _write_l0_result(
            context,
            target,
            out_dir,
            metrics={"reason": "missing_backtest_data"},
            trade_log=pd.DataFrame(),
            daily_pnl=pd.DataFrame(),
            equity_curve=pd.DataFrame(),
        )

    trade_log, daily_pnl, equity_curve, metrics = target_artifacts(context, target, backtest)
    context.cache[f"{target}:trade_log"] = trade_log
    context.cache[f"{target}:daily_pnl"] = daily_pnl
    context.cache[f"{target}:equity_curve"] = equity_curve
    context.cache[f"{target}:l0_metrics"] = metrics
    context.cache[f"{target}:data_warnings"] = backtest.data_warnings
    return _write_l0_result(
        context,
        target,
        out_dir,
        metrics=metrics,
        trade_log=trade_log,
        daily_pnl=daily_pnl,
        equity_curve=equity_curve,
        data_warnings=backtest.data_warnings,
    )


def evaluate_l0_pass(metrics: dict[str, Any], cfg: Any) -> tuple[str, bool, str]:
    threshold = cfg.validation.l0_pure_backtest
    total_trades = int(metrics.get("total_trades", 0) or 0)
    insufficient = total_trades <= int(threshold.trades_min)
    round_trip_cost = _round_trip_cost(metrics)
    avg_cost_threshold = (
        float(threshold.avg_trade_to_round_trip_cost_min) * round_trip_cost
    )
    checks = {
        "profit_factor <= min": float(metrics.get("profit_factor", 0.0) or 0.0)
        > float(threshold.profit_factor_min),
        "sharpe <= min": float(metrics.get("sharpe_ratio", 0.0) or 0.0)
        > float(threshold.sharpe_min),
        "max_drawdown >= max": float(metrics.get("max_drawdown_pct", 100.0) or 100.0)
        < float(threshold.mdd_max_pct),
        "calmar <= min": float(metrics.get("calmar_ratio", 0.0) or 0.0)
        > float(threshold.calmar_min),
        "average_trade_pnl <= cost threshold": float(metrics.get("average_trade_pnl", 0.0) or 0.0)
        > avg_cost_threshold,
        "final_net_profit <= 0": float(metrics.get("net_profit", 0.0) or 0.0) > 0.0,
    }
    return status_from_checks(checks, insufficient=insufficient)


def _get_or_run_backtest(context: ValidationContext, target: str) -> BacktestResult | None:
    cached = context.cache.get("full_r3_portfolio:backtest_result")
    if cached is not None:
        return cached
    if not context.data_by_symbol:
        return None
    result = BacktestEngine(context.cfg, initial_capital=context.initial_capital).run(
        data_by_symbol=context.data_by_symbol,
        funding_by_symbol=context.funding_by_symbol,
        premium_by_symbol=context.premium_by_symbol,
    )
    context.cache["full_r3_portfolio:backtest_result"] = result
    return result


def _write_l0_result(
    context: ValidationContext,
    target: str,
    out_dir: Path,
    *,
    metrics: dict[str, Any],
    trade_log: pd.DataFrame,
    daily_pnl: pd.DataFrame,
    equity_curve: pd.DataFrame,
    data_warnings: list[str] | None = None,
) -> LevelResult:
    status, passed, failure_reason = evaluate_l0_pass(metrics, context.cfg)
    paths = {
        "metrics": write_json(out_dir / "L0_metrics.json", metrics),
        "trade_log": write_csv(out_dir / "L0_trade_log.csv", trade_log),
        "daily_pnl": write_csv(out_dir / "L0_daily_pnl.csv", daily_pnl),
        "equity_curve": write_csv(out_dir / "L0_equity_curve.csv", equity_curve),
    }
    lines = [
        f"- Target: `{target}`",
        f"- Status: `{status}`",
        f"- Failure reason: `{failure_reason or 'none'}`",
        f"- Total trades: `{metrics.get('total_trades', 0)}`",
        f"- Profit factor: `{metrics.get('profit_factor', 0)}`",
        f"- Sharpe ratio: `{metrics.get('sharpe_ratio', 0)}`",
        f"- Max drawdown pct: `{metrics.get('max_drawdown_pct', 0)}`",
        f"- Net profit: `{metrics.get('net_profit', 0)}`",
    ]
    paths["report"] = write_markdown(out_dir / "L0_report.md", "L0 Pure Backtest", lines)
    return LevelResult(
        target=target,
        level="L0",
        test_name="pure_backtest",
        status=status,
        passed=passed,
        key_metrics=metrics,
        failure_reason=failure_reason,
        artifacts=result_artifacts(paths),
        data_warnings=data_warnings or [],
    )


def _round_trip_cost(metrics: dict[str, Any]) -> float:
    trades = int(metrics.get("total_trades", 0) or 0)
    if trades <= 0:
        return 0.0
    return (
        float(metrics.get("total_fees", 0.0) or 0.0)
        + float(metrics.get("total_slippage", 0.0) or 0.0)
        + abs(float(metrics.get("total_funding", 0.0) or 0.0))
    ) / trades

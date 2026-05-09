"""L5 final OOS validation."""
from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from ..backtest_engine import BacktestEngine
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


def split_final_oos(
    data_by_symbol: dict[str, dict[str, pd.DataFrame]],
    *,
    oos_fraction: float = 0.20,
) -> tuple[dict[str, dict[str, pd.DataFrame]], dict[str, dict[str, pd.DataFrame]], pd.Timestamp]:
    all_times = []
    for frames in data_by_symbol.values():
        if "5m" in frames and not frames["5m"].empty:
            all_times.extend(frames["5m"].index)
    if not all_times:
        empty = {symbol: {tf: frame.iloc[0:0].copy() for tf, frame in frames.items()} for symbol, frames in data_by_symbol.items()}
        return empty, empty, pd.Timestamp.min
    ordered = pd.DatetimeIndex(sorted(pd.Timestamp(ts) for ts in all_times)).unique()
    split_idx = max(int(len(ordered) * (1.0 - oos_fraction)), 1)
    split_time = ordered[min(split_idx, len(ordered) - 1)]
    insample: dict[str, dict[str, pd.DataFrame]] = {}
    oos: dict[str, dict[str, pd.DataFrame]] = {}
    for symbol, frames in data_by_symbol.items():
        insample[symbol] = {}
        oos[symbol] = {}
        for timeframe, frame in frames.items():
            insample[symbol][timeframe] = frame.loc[frame.index < split_time].copy()
            oos[symbol][timeframe] = frame.loc[frame.index >= split_time].copy()
    return insample, oos, split_time


def run_l5(context: ValidationContext, target: str) -> LevelResult:
    out_dir = context.child_output_dir(target, "L5")
    if not context.data_by_symbol:
        return _insufficient(context, target, out_dir, "missing_data")
    _, oos_data, split_time = split_final_oos(context.data_by_symbol)
    if any(frames["5m"].empty for frames in oos_data.values()):
        return _insufficient(context, target, out_dir, "empty_oos_window")

    result = BacktestEngine(context.cfg, initial_capital=context.initial_capital).run(
        data_by_symbol=oos_data,
        funding_by_symbol=_slice_aux(context.funding_by_symbol, split_time),
        premium_by_symbol=_slice_aux(context.premium_by_symbol, split_time),
    )
    trade_log, daily_pnl, _, target_metrics = target_artifacts(context, target, result)
    metrics = {
        **target_metrics,
        "oos_split_time": split_time.isoformat(),
        "p10_daily_pnl": _daily_quantile(daily_pnl, 0.10),
        "p90_daily_pnl": _daily_quantile(daily_pnl, 0.90),
        "daily_target_50_150u": _daily_target_band(daily_pnl),
    }
    threshold = context.cfg.validation.l5_final_oos
    insufficient = int(metrics.get("total_trades", 0) or 0) == 0
    checks = {
        "oos_profit_factor <= min": float(metrics.get("profit_factor", 0.0) or 0.0) > float(threshold.profit_factor_min),
        "oos_sharpe <= min": float(metrics.get("sharpe_ratio", 0.0) or 0.0) > float(threshold.sharpe_min),
        "oos_max_drawdown >= 20": float(metrics.get("max_drawdown_pct", 100.0) or 100.0) < 20.0,
        "oos_net_profit <= 0": float(metrics.get("net_profit", 0.0) or 0.0) > 0.0,
        "oos_average_daily_pnl <= 0": float(metrics.get("average_daily_pnl", 0.0) or 0.0) > 0.0,
    }
    status, passed, failure_reason = status_from_checks(checks, insufficient=insufficient)
    paths = {
        "report": write_markdown(
            out_dir / "L5_report.md",
            "L5 Final OOS",
            [
                f"- Target: `{target}`",
                f"- Status: `{status}`",
                f"- Split time: `{split_time}`",
                f"- Failure reason: `{failure_reason or 'none'}`",
            ],
        ),
        "trade_log": write_csv(out_dir / "final_oos_trade_log.csv", trade_log),
        "daily_pnl": write_csv(out_dir / "final_oos_daily_pnl.csv", daily_pnl),
        "metrics": write_json(out_dir / "final_oos_metrics.json", metrics),
    }
    return LevelResult(
        target=target,
        level="L5",
        test_name="final_oos",
        status=status,
        passed=passed,
        key_metrics=metrics,
        failure_reason=failure_reason,
        artifacts=result_artifacts(paths),
        data_warnings=result.data_warnings,
    )


def _insufficient(context: ValidationContext, target: str, out_dir, reason: str) -> LevelResult:
    metrics = {"reason": reason}
    paths = {
        "report": write_markdown(out_dir / "L5_report.md", "L5 Final OOS", [f"- Status: `INSUFFICIENT_DATA`", f"- Reason: `{reason}`"]),
        "trade_log": write_csv(out_dir / "final_oos_trade_log.csv", pd.DataFrame()),
        "daily_pnl": write_csv(out_dir / "final_oos_daily_pnl.csv", pd.DataFrame()),
        "metrics": write_json(out_dir / "final_oos_metrics.json", metrics),
    }
    return LevelResult(
        target=target,
        level="L5",
        test_name="final_oos",
        status="INSUFFICIENT_DATA",
        passed=False,
        key_metrics=metrics,
        failure_reason="INSUFFICIENT_DATA",
        artifacts=result_artifacts(paths),
    )


def _slice_aux(frames_by_symbol: dict[str, pd.DataFrame] | None, split_time: pd.Timestamp):
    if not frames_by_symbol:
        return None
    return {
        symbol: frame.loc[frame.index >= split_time].copy()
        for symbol, frame in frames_by_symbol.items()
    }


def _daily_quantile(daily_pnl: pd.DataFrame, q: float) -> float:
    if daily_pnl.empty or "daily_pnl" not in daily_pnl:
        return 0.0
    return float(pd.to_numeric(daily_pnl["daily_pnl"], errors="coerce").fillna(0.0).quantile(q))


def _daily_target_band(daily_pnl: pd.DataFrame) -> dict[str, Any]:
    if daily_pnl.empty or "daily_pnl" not in daily_pnl:
        return {"days_in_band": 0, "ratio": 0.0}
    values = pd.to_numeric(daily_pnl["daily_pnl"], errors="coerce").fillna(0.0)
    mask = (values >= 50.0) & (values <= 150.0)
    return {"days_in_band": int(mask.sum()), "ratio": float(mask.mean()) if len(mask) else 0.0}

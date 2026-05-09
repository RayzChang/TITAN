"""L1 walk-forward stability validation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from ..backtest_engine import BacktestEngine
from .common import (
    LevelResult,
    ValidationContext,
    result_artifacts,
    status_from_checks,
    write_csv,
    write_markdown,
)
from .target_metrics import target_artifacts


@dataclass(frozen=True)
class WalkForwardFold:
    fold_id: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime


def build_walk_forward_folds(
    *,
    start: datetime,
    end: datetime,
    train_days: int,
    test_days: int,
    step_days: int,
) -> list[WalkForwardFold]:
    folds: list[WalkForwardFold] = []
    cursor = start
    fold_id = 1
    while True:
        train_start = cursor
        train_end = train_start + timedelta(days=train_days)
        test_start = train_end
        test_end = test_start + timedelta(days=test_days)
        if test_end > end:
            break
        folds.append(WalkForwardFold(fold_id, train_start, train_end, test_start, test_end))
        fold_id += 1
        cursor = cursor + timedelta(days=step_days)
    return folds


def aggregate_fold_metrics(fold_metrics: pd.DataFrame) -> dict[str, float]:
    if fold_metrics.empty:
        return {
            "oos_sharpe": 0.0,
            "oos_profit_factor": 0.0,
            "oos_is_return_ratio": 0.0,
            "positive_fold_ratio": 0.0,
        }
    is_return = fold_metrics["is_total_return_pct"].replace(0.0, pd.NA)
    ratio = (fold_metrics["oos_total_return_pct"] / is_return).dropna()
    return {
        "oos_sharpe": float(fold_metrics["oos_sharpe_ratio"].mean()),
        "oos_profit_factor": float(fold_metrics["oos_profit_factor"].mean()),
        "oos_is_return_ratio": float(ratio.mean()) if not ratio.empty else 0.0,
        "positive_fold_ratio": float((fold_metrics["oos_net_profit"] > 0).mean()),
    }


def run_l1(context: ValidationContext, target: str) -> LevelResult:
    out_dir = context.child_output_dir(target, "L1")
    folds = _folds_from_context(context)
    fold_rows: list[dict[str, Any]] = []
    parameter_rows: list[dict[str, Any]] = []
    if folds and context.data_by_symbol:
        max_folds = 2 if context.max_runtime_smoke else len(folds)
        for fold in folds[:max_folds]:
            is_metrics = _run_window(context, target, fold.train_start, fold.train_end)
            oos_metrics = _run_window(context, target, fold.test_start, fold.test_end)
            if is_metrics is None or oos_metrics is None:
                continue
            fold_rows.append({
                "fold_id": fold.fold_id,
                "train_start": fold.train_start.isoformat(),
                "train_end": fold.train_end.isoformat(),
                "test_start": fold.test_start.isoformat(),
                "test_end": fold.test_end.isoformat(),
                "is_total_return_pct": is_metrics.get("total_return_pct", 0.0),
                "oos_total_return_pct": oos_metrics.get("total_return_pct", 0.0),
                "oos_net_profit": oos_metrics.get("net_profit", 0.0),
                "oos_sharpe_ratio": oos_metrics.get("sharpe_ratio", 0.0),
                "oos_profit_factor": oos_metrics.get("profit_factor", 0.0),
            })
            parameter_rows.append({
                "fold_id": fold.fold_id,
                "parameter_set": "spec_locked",
                "changed": False,
            })

    fold_metrics = pd.DataFrame(fold_rows)
    summary = aggregate_fold_metrics(fold_metrics)
    parameter_stability = pd.DataFrame(parameter_rows)
    if parameter_stability.empty:
        parameter_stability = pd.DataFrame(columns=["fold_id", "parameter_set", "changed"])
    summary_frame = pd.DataFrame([summary])

    insufficient = fold_metrics.empty
    threshold = context.cfg.validation.l1_walk_forward
    checks = {
        "oos_sharpe <= min": summary["oos_sharpe"] > float(threshold.aggregated_oos_sharpe_min),
        "oos_profit_factor <= min": summary["oos_profit_factor"] > 1.08,
        "oos_is_return_ratio <= min": summary["oos_is_return_ratio"] > float(threshold.oos_to_is_cagr_ratio_min),
        "positive_fold_ratio < 60_pct": summary["positive_fold_ratio"] >= 0.60,
        "parameters_unstable": not bool(parameter_stability["changed"].any()) if not parameter_stability.empty else True,
    }
    status, passed, failure_reason = status_from_checks(checks, insufficient=insufficient)

    paths = {
        "report": write_markdown(
            out_dir / "L1_report.md",
            "L1 Walk-Forward",
            [
                f"- Target: `{target}`",
                f"- Status: `{status}`",
                f"- Folds: `{len(fold_metrics)}`",
                f"- Failure reason: `{failure_reason or 'none'}`",
            ],
        ),
        "summary": write_csv(out_dir / "walk_forward_summary.csv", summary_frame),
        "fold_metrics": write_csv(out_dir / "fold_metrics.csv", fold_metrics),
        "parameter_stability": write_csv(out_dir / "parameter_stability.csv", parameter_stability),
    }
    return LevelResult(
        target=target,
        level="L1",
        test_name="walk_forward",
        status=status,
        passed=passed,
        key_metrics={**summary, "fold_count": len(fold_metrics)},
        failure_reason=failure_reason,
        artifacts=result_artifacts(paths),
    )


def _folds_from_context(context: ValidationContext) -> list[WalkForwardFold]:
    if context.start is None or context.end is None:
        return []
    cfg = context.cfg.validation.l1_walk_forward
    if context.max_runtime_smoke:
        train_days = min(7, int(cfg.train_days))
        test_days = min(3, int(cfg.test_days))
        step_days = min(3, int(cfg.step_days))
    else:
        train_days = int(cfg.train_days)
        test_days = int(cfg.test_days)
        step_days = int(cfg.step_days)
    return build_walk_forward_folds(
        start=context.start,
        end=context.end,
        train_days=train_days,
        test_days=test_days,
        step_days=step_days,
    )


def _run_window(context: ValidationContext, target: str, start: datetime, end: datetime):
    if not context.data_by_symbol:
        return None
    data = _slice_data(context.data_by_symbol, start, end)
    if any(frames["5m"].empty for frames in data.values()):
        return None
    funding = _slice_aux(context.funding_by_symbol, start, end)
    premium = _slice_aux(context.premium_by_symbol, start, end)
    result = BacktestEngine(context.cfg, initial_capital=context.initial_capital).run(
        data_by_symbol=data,
        funding_by_symbol=funding,
        premium_by_symbol=premium,
    )
    return target_artifacts(context, target, result)[3]


def _slice_data(
    data_by_symbol: dict[str, dict[str, pd.DataFrame]],
    start: datetime,
    end: datetime,
) -> dict[str, dict[str, pd.DataFrame]]:
    out: dict[str, dict[str, pd.DataFrame]] = {}
    for symbol, frames in data_by_symbol.items():
        out[symbol] = {
            timeframe: frame.loc[(frame.index >= start) & (frame.index <= end)].copy()
            for timeframe, frame in frames.items()
        }
    return out


def _slice_aux(
    frames_by_symbol: dict[str, pd.DataFrame] | None,
    start: datetime,
    end: datetime,
) -> dict[str, pd.DataFrame] | None:
    if not frames_by_symbol:
        return None
    return {
        symbol: frame.loc[(frame.index >= start) & (frame.index <= end)].copy()
        for symbol, frame in frames_by_symbol.items()
    }

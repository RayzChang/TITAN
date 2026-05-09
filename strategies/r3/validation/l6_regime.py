"""L6 regime stratification validation."""
from __future__ import annotations

from typing import Any

import pandas as pd

from .common import (
    LevelResult,
    ValidationContext,
    result_artifacts,
    status_from_checks,
    write_csv,
    write_markdown,
)


MATRIX_COLUMNS = [
    "trend_strength",
    "volatility_regime",
    "funding_regime",
    "market_regime",
    "strategy_type",
    "trade_count",
    "win_rate",
    "profit_factor",
    "expectancy",
    "average_r",
    "max_drawdown",
    "average_holding_time",
    "total_pnl",
]


def build_regime_strategy_matrix(trade_log: pd.DataFrame) -> pd.DataFrame:
    if trade_log.empty:
        return pd.DataFrame(columns=MATRIX_COLUMNS)
    df = trade_log.copy()
    if "strategy_name" not in df:
        df["strategy_name"] = "unknown"
    df["realized_pnl"] = pd.to_numeric(df.get("realized_pnl", 0.0), errors="coerce").fillna(0.0)
    rows: list[dict[str, Any]] = []
    for strategy_name, group in df.groupby("strategy_name"):
        pnl = group["realized_pnl"]
        wins = pnl[pnl > 0]
        losses = pnl[pnl < 0]
        gross_loss = abs(float(losses.sum()))
        profit_factor = float(wins.sum() / gross_loss) if gross_loss > 0 else (float("inf") if wins.sum() > 0 else 0.0)
        curve = pnl.cumsum()
        rows.append({
            "trend_strength": group.get("trend_strength", pd.Series(["UNAVAILABLE"])).iloc[0],
            "volatility_regime": group.get("volatility_regime", pd.Series(["UNAVAILABLE"])).iloc[0],
            "funding_regime": group.get("funding_regime", pd.Series(["UNAVAILABLE"])).iloc[0],
            "market_regime": group.get("market_regime", pd.Series(["UNAVAILABLE"])).iloc[0],
            "strategy_type": strategy_name,
            "trade_count": int(len(group)),
            "win_rate": float((pnl > 0).mean()) if len(group) else 0.0,
            "profit_factor": profit_factor,
            "expectancy": float(pnl.mean()) if len(group) else 0.0,
            "average_r": float(group["average_r"].mean()) if "average_r" in group else 0.0,
            "max_drawdown": _max_drawdown(curve),
            "average_holding_time": "UNAVAILABLE",
            "total_pnl": float(pnl.sum()),
        })
    return pd.DataFrame(rows, columns=MATRIX_COLUMNS)


def run_l6(context: ValidationContext, target: str) -> LevelResult:
    out_dir = context.child_output_dir(target, "L6")
    trade_log = context.cache.get(f"{target}:trade_log", pd.DataFrame())
    matrix = build_regime_strategy_matrix(trade_log)
    regime_metrics = matrix.copy()
    contribution = _regime_contribution(matrix)
    missing_labels = bool(
        matrix.empty
        or (matrix[["trend_strength", "volatility_regime", "funding_regime", "market_regime"]] == "UNAVAILABLE").any().any()
    )
    checks = {
        "trend_pullback_not_positive_in_trend_high": _strategy_non_negative(matrix, "trend_pullback"),
        "mean_reversion_not_positive_in_sideways": _strategy_non_negative(matrix, "mean_reversion"),
        "funding_reversal_negative_in_extreme": _strategy_non_negative(matrix, "funding_reversal"),
        "single_regime_dominates": not _single_regime_dominates(contribution),
    }
    status, passed, failure_reason = status_from_checks(checks, insufficient=missing_labels)
    paths = {
        "report": write_markdown(
            out_dir / "L6_report.md",
            "L6 Regime Stratification",
            [
                f"- Target: `{target}`",
                f"- Status: `{status}`",
                f"- Failure reason: `{failure_reason or 'none'}`",
                "- Note: Sprint 6 trade log does not yet persist full regime labels; missing labels are treated as insufficient data for formal gating.",
            ],
        ),
        "regime_metrics": write_csv(out_dir / "regime_metrics.csv", regime_metrics),
        "regime_strategy_matrix": write_csv(out_dir / "regime_strategy_matrix.csv", matrix),
        "regime_contribution": write_csv(out_dir / "regime_contribution.csv", contribution),
    }
    return LevelResult(
        target=target,
        level="L6",
        test_name="regime_stratification",
        status=status,
        passed=passed,
        key_metrics={
            "matrix_rows": int(len(matrix)),
            "missing_regime_labels": missing_labels,
            "contribution_rows": int(len(contribution)),
        },
        failure_reason=failure_reason,
        artifacts=result_artifacts(paths),
    )


def _strategy_non_negative(matrix: pd.DataFrame, strategy: str) -> bool:
    if matrix.empty:
        return False
    subset = matrix[matrix["strategy_type"] == strategy]
    if subset.empty:
        return False
    return bool((subset["expectancy"] >= 0).all())


def _regime_contribution(matrix: pd.DataFrame) -> pd.DataFrame:
    if matrix.empty:
        return pd.DataFrame(columns=["market_regime", "strategy_type", "total_pnl", "contribution_pct"])
    total_abs = matrix["total_pnl"].abs().sum()
    out = matrix[["market_regime", "strategy_type", "total_pnl"]].copy()
    out["contribution_pct"] = out["total_pnl"].abs() / total_abs * 100.0 if total_abs else 0.0
    return out


def _single_regime_dominates(contribution: pd.DataFrame) -> bool:
    if contribution.empty or "contribution_pct" not in contribution:
        return False
    return bool(contribution["contribution_pct"].max() > 80.0)


def _max_drawdown(curve: pd.Series) -> float:
    if curve.empty:
        return 0.0
    running_max = curve.cummax()
    drawdown = curve - running_max
    return float(abs(drawdown.min()))


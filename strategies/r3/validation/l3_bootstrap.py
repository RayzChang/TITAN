"""L3 block bootstrap validation."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .common import (
    LevelResult,
    ValidationContext,
    result_artifacts,
    status_from_checks,
    write_csv,
    write_json,
    write_markdown,
)


BLOCK_SIZES = [5, 10, 20]


def run_l3(context: ValidationContext, target: str) -> LevelResult:
    out_dir = context.child_output_dir(target, "L3")
    trade_log = context.cache.get(f"{target}:trade_log", pd.DataFrame())
    returns = _returns_from_trade_log(trade_log)
    results = run_block_bootstrap(
        returns=returns,
        initial_capital=context.initial_capital,
        simulations=context.simulations,
        seed=context.seed,
        block_sizes=BLOCK_SIZES,
    )
    metrics = bootstrap_metrics(results, context.initial_capital)
    context.cache[f"{target}:bootstrap_results"] = results
    context.cache[f"{target}:bootstrap_metrics"] = metrics
    by_block = (
        results.groupby("block_size", as_index=False)
        .agg(
            pct5_final_equity=("final_equity", lambda s: float(s.quantile(0.05))),
            pct95_max_drawdown_pct=("max_drawdown_pct", lambda s: float(s.quantile(0.95))),
            pct5_expectancy=("expectancy", lambda s: float(s.quantile(0.05))),
        )
        if not results.empty
        else pd.DataFrame(columns=["block_size", "pct5_final_equity", "pct95_max_drawdown_pct", "pct5_expectancy"])
    )

    insufficient = results.empty
    checks = {
        "pct5_expectancy <= 0": metrics.get("pct5_expectancy", 0.0) > 0.0,
        "pct5_final_equity <= initial": metrics.get("pct5_final_equity", 0.0) > context.initial_capital,
        "pct95_max_drawdown >= 30": metrics.get("pct95_max_drawdown_pct", 100.0) < 30.0,
        "block_size_failure": bool(metrics.get("all_block_sizes_viable", False)),
    }
    status, passed, failure_reason = status_from_checks(checks, insufficient=insufficient)
    paths = {
        "report": write_markdown(
            out_dir / "L3_report.md",
            "L3 Block Bootstrap",
            [
                f"- Target: `{target}`",
                f"- Status: `{status}`",
                f"- Simulations: `{len(results)}`",
                f"- Failure reason: `{failure_reason or 'none'}`",
            ],
        ),
        "results": write_csv(out_dir / "bootstrap_results.csv", results),
        "by_block_size": write_csv(out_dir / "bootstrap_by_block_size.csv", by_block),
        "metrics": write_json(out_dir / "bootstrap_metrics.json", metrics),
    }
    return LevelResult(
        target=target,
        level="L3",
        test_name="block_bootstrap",
        status=status,
        passed=passed,
        key_metrics=metrics,
        failure_reason=failure_reason,
        artifacts=result_artifacts(paths),
    )


def run_block_bootstrap(
    *,
    returns: np.ndarray,
    initial_capital: float,
    simulations: int,
    seed: int,
    block_sizes: list[int],
) -> pd.DataFrame:
    if len(returns) < min(block_sizes):
        return pd.DataFrame(columns=[
            "simulation", "block_size", "final_equity", "expectancy", "max_drawdown_pct",
        ])
    rng = np.random.default_rng(seed)
    rows: list[dict[str, Any]] = []
    for block_size in block_sizes:
        if len(returns) < block_size:
            continue
        blocks = _blocks(returns, block_size)
        for i in range(int(simulations)):
            sample: list[float] = []
            while len(sample) < len(returns):
                block = blocks[rng.integers(0, len(blocks))]
                sample.extend(block)
            sample_array = np.array(sample[: len(returns)], dtype=float)
            curve = initial_capital + np.cumsum(sample_array)
            rows.append({
                "simulation": i + 1,
                "block_size": block_size,
                "final_equity": float(initial_capital + sample_array.sum()),
                "expectancy": float(sample_array.mean()),
                "max_drawdown_pct": _max_drawdown_pct(curve, initial_capital),
            })
    return pd.DataFrame(rows)


def bootstrap_metrics(results: pd.DataFrame, initial_capital: float) -> dict[str, Any]:
    if results.empty:
        return {
            "pct5_expectancy": 0.0,
            "pct5_final_equity": 0.0,
            "pct95_max_drawdown_pct": 100.0,
            "all_block_sizes_viable": False,
        }
    by_block = results.groupby("block_size")["final_equity"].quantile(0.05)
    return {
        "pct5_expectancy": float(results["expectancy"].quantile(0.05)),
        "pct5_final_equity": float(results["final_equity"].quantile(0.05)),
        "pct95_max_drawdown_pct": float(results["max_drawdown_pct"].quantile(0.95)),
        "all_block_sizes_viable": bool((by_block > initial_capital).all()),
    }


def _returns_from_trade_log(trade_log: pd.DataFrame) -> np.ndarray:
    if trade_log.empty or "realized_pnl" not in trade_log:
        return np.array([], dtype=float)
    return pd.to_numeric(trade_log["realized_pnl"], errors="coerce").dropna().to_numpy(dtype=float)


def _blocks(values: np.ndarray, block_size: int) -> list[np.ndarray]:
    return [values[i : i + block_size] for i in range(0, len(values) - block_size + 1)]


def _max_drawdown_pct(curve: np.ndarray, initial_capital: float) -> float:
    if len(curve) == 0:
        return 0.0
    full_curve = np.r_[initial_capital, curve]
    running_max = np.maximum.accumulate(full_curve)
    drawdowns = (running_max - full_curve) / running_max * 100.0
    return float(np.nanmax(drawdowns))


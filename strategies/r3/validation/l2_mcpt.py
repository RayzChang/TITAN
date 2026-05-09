"""L2 Monte Carlo perturbation test."""
from __future__ import annotations

from pathlib import Path
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


def run_l2(context: ValidationContext, target: str) -> LevelResult:
    out_dir = context.child_output_dir(target, "L2")
    trade_log = context.cache.get(f"{target}:trade_log", pd.DataFrame())
    results = run_mcpt(
        trade_log=trade_log,
        initial_capital=context.initial_capital,
        simulations=context.simulations,
        seed=context.seed,
    )
    metrics = mcpt_metrics(results, context.initial_capital)
    context.cache[f"{target}:mcpt_results"] = results
    context.cache[f"{target}:mcpt_metrics"] = metrics

    insufficient = results.empty
    cfg = context.cfg.validation.l2_mcpt
    checks = {
        "pct5_final_equity <= initial": metrics.get("pct5_final_equity", 0.0) > context.initial_capital,
        "pct5_cagr <= min": metrics.get("pct5_cagr", 0.0) > float(cfg.pct5_cagr_min),
        "pct95_max_drawdown >= 30": metrics.get("pct95_max_drawdown_pct", 100.0) < 30.0,
        "risk_of_ruin >= max": metrics.get("risk_of_ruin", 1.0) < float(cfg.risk_of_ruin_max),
        "median_profit_factor <= min": metrics.get("median_profit_factor", 0.0) > 1.05,
    }
    status, passed, failure_reason = status_from_checks(checks, insufficient=insufficient)
    paths = {
        "report": write_markdown(
            out_dir / "L2_report.md",
            "L2 MCPT",
            [
                f"- Target: `{target}`",
                f"- Status: `{status}`",
                f"- Simulations: `{len(results)}`",
                f"- Failure reason: `{failure_reason or 'none'}`",
                "- Distribution image: skipped in Sprint 7 when plotting backend is unavailable.",
            ],
        ),
        "results": write_csv(out_dir / "mcpt_results.csv", results),
        "metrics": write_json(out_dir / "mcpt_metrics.json", metrics),
    }
    return LevelResult(
        target=target,
        level="L2",
        test_name="mcpt",
        status=status,
        passed=passed,
        key_metrics=metrics,
        failure_reason=failure_reason,
        artifacts=result_artifacts(paths),
    )


def run_mcpt(
    *,
    trade_log: pd.DataFrame,
    initial_capital: float,
    simulations: int,
    seed: int,
) -> pd.DataFrame:
    if trade_log.empty or "realized_pnl" not in trade_log.columns or len(trade_log) < 2:
        return pd.DataFrame(columns=[
            "simulation", "final_equity", "cagr", "max_drawdown_pct",
            "profit_factor", "risk_of_ruin_event", "extra_slippage_bps",
            "missed_order_probability", "fee_multiplier", "funding_multiplier",
        ])
    rng = np.random.default_rng(seed)
    base_pnl = pd.to_numeric(trade_log["realized_pnl"], errors="coerce").fillna(0.0).to_numpy()
    fees = _column(trade_log, "fee")
    slippage = _column(trade_log, "slippage")
    funding = _column(trade_log, "funding_cost")
    rows: list[dict[str, Any]] = []
    for i in range(int(simulations)):
        extra_slippage_bps = rng.uniform(0.0, 3.0)
        entry_delay = rng.integers(0, 2, size=len(base_pnl))
        exit_delay = rng.integers(0, 2, size=len(base_pnl))
        missed_probability = rng.uniform(0.0, 0.05)
        fee_multiplier = rng.uniform(1.0, 1.5)
        funding_multiplier = rng.uniform(1.0, 1.5)
        keep_mask = rng.random(len(base_pnl)) >= missed_probability

        delay_penalty = np.abs(base_pnl) * (entry_delay + exit_delay) * 0.0025
        extra_slippage = np.abs(base_pnl) * (extra_slippage_bps / 10000.0)
        stressed = (
            base_pnl
            - fees * (fee_multiplier - 1.0)
            - np.abs(slippage)
            - extra_slippage
            - delay_penalty
            - np.abs(funding) * (funding_multiplier - 1.0)
        )
        stressed = stressed[keep_mask]
        final_equity = float(initial_capital + stressed.sum())
        curve = initial_capital + np.cumsum(stressed)
        max_dd = _max_drawdown_pct(curve, initial_capital)
        rows.append({
            "simulation": i + 1,
            "final_equity": final_equity,
            "cagr": (final_equity / initial_capital - 1.0) if initial_capital else 0.0,
            "max_drawdown_pct": max_dd,
            "profit_factor": _profit_factor(stressed),
            "risk_of_ruin_event": final_equity <= initial_capital * 0.5,
            "extra_slippage_bps": extra_slippage_bps,
            "missed_order_probability": missed_probability,
            "fee_multiplier": fee_multiplier,
            "funding_multiplier": funding_multiplier,
        })
    return pd.DataFrame(rows)


def mcpt_metrics(results: pd.DataFrame, initial_capital: float) -> dict[str, Any]:
    if results.empty:
        return {
            "simulations": 0,
            "pct5_final_equity": 0.0,
            "pct5_cagr": 0.0,
            "pct95_max_drawdown_pct": 100.0,
            "risk_of_ruin": 1.0,
            "median_profit_factor": 0.0,
            "raw_p_value": 1.0,
        }
    return {
        "simulations": int(len(results)),
        "pct5_final_equity": float(results["final_equity"].quantile(0.05)),
        "pct5_cagr": float(results["cagr"].quantile(0.05)),
        "pct95_max_drawdown_pct": float(results["max_drawdown_pct"].quantile(0.95)),
        "risk_of_ruin": float(results["risk_of_ruin_event"].mean()),
        "median_profit_factor": float(results["profit_factor"].median()),
        "raw_p_value": float((results["final_equity"] <= initial_capital).mean()),
    }


def _column(frame: pd.DataFrame, name: str) -> np.ndarray:
    if name not in frame:
        return np.zeros(len(frame))
    return pd.to_numeric(frame[name], errors="coerce").fillna(0.0).to_numpy()


def _profit_factor(values: np.ndarray) -> float:
    wins = values[values > 0].sum()
    losses = abs(values[values < 0].sum())
    if losses == 0:
        return float("inf") if wins > 0 else 0.0
    return float(wins / losses)


def _max_drawdown_pct(curve: np.ndarray, initial_capital: float) -> float:
    if len(curve) == 0:
        return 0.0
    full_curve = np.r_[initial_capital, curve]
    running_max = np.maximum.accumulate(full_curve)
    drawdowns = (running_max - full_curve) / running_max * 100.0
    return float(np.nanmax(drawdowns))


"""L4 multiple-testing correction validation."""
from __future__ import annotations

from typing import Any

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


def bonferroni_correction(raw_p_value: float, combinations: int, alpha: float = 0.05) -> dict[str, float]:
    n = max(int(combinations), 1)
    corrected_alpha = float(alpha) / n
    corrected_p_value = min(float(raw_p_value) * n, 1.0)
    return {
        "raw_p_value": float(raw_p_value),
        "parameter_combinations": n,
        "corrected_alpha": corrected_alpha,
        "corrected_p_value": corrected_p_value,
    }


def run_l4(context: ValidationContext, target: str) -> LevelResult:
    out_dir = context.child_output_dir(target, "L4")
    mcpt_metrics = context.cache.get(f"{target}:mcpt_metrics", {})
    raw_p = float(mcpt_metrics.get("raw_p_value", 1.0))
    combo_count = int(
        getattr(context.cfg.validation.l4_multiple_testing, "parameter_combinations_tested", 1)
    )
    alpha = float(context.cfg.validation.l4_multiple_testing.bonferroni_alpha)
    corrected = bonferroni_correction(raw_p, combo_count, alpha)
    grid = pd.DataFrame([{
        "parameter_set": "spec_locked",
        "parameter_combinations_counted": corrected["parameter_combinations"],
        "raw_p_value": corrected["raw_p_value"],
        "corrected_p_value": corrected["corrected_p_value"],
        "expectancy_positive": bool(mcpt_metrics.get("median_profit_factor", 0.0) > 1.0),
    }])
    heatmap = pd.DataFrame([{
        "parameter_axis_x": "spec_locked",
        "parameter_axis_y": "spec_locked",
        "expectancy": mcpt_metrics.get("median_profit_factor", 0.0),
    }])
    stability = pd.DataFrame([{
        "region": "spec_locked_neighborhood",
        "positive_expectancy": bool(grid["expectancy_positive"].all()),
        "notes": "No tuning grid beyond locked R3 spec in Sprint 7.",
    }])
    metrics = {
        **corrected,
        "neighboring_region_positive": bool(stability["positive_expectancy"].all()),
    }
    insufficient = raw_p >= 1.0 and not mcpt_metrics
    checks = {
        "corrected_p_value >= 0.05": metrics["corrected_p_value"] < 0.05,
        "neighbor_region_not_positive": metrics["neighboring_region_positive"],
    }
    status, passed, failure_reason = status_from_checks(checks, insufficient=insufficient)
    paths = {
        "report": write_markdown(
            out_dir / "L4_report.md",
            "L4 Bonferroni",
            [
                f"- Target: `{target}`",
                f"- Status: `{status}`",
                f"- Parameter combinations counted: `{metrics['parameter_combinations']}`",
                f"- Corrected p-value: `{metrics['corrected_p_value']}`",
                f"- Failure reason: `{failure_reason or 'none'}`",
            ],
        ),
        "parameter_grid_results": write_csv(out_dir / "parameter_grid_results.csv", grid),
        "bonferroni_report": write_json(out_dir / "bonferroni_report.json", metrics),
        "parameter_heatmap": write_csv(out_dir / "parameter_heatmap.csv", heatmap),
        "parameter_stability_summary": write_csv(out_dir / "parameter_stability_summary.csv", stability),
    }
    return LevelResult(
        target=target,
        level="L4",
        test_name="bonferroni",
        status=status,
        passed=passed,
        key_metrics=metrics,
        failure_reason=failure_reason,
        artifacts=result_artifacts(paths),
    )


"""Final report writers for R3 Sprint 7 validation."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from .common import (
    ValidationRunResult,
    result_artifacts,
    write_csv,
    write_json,
    write_markdown,
)


def write_validation_reports(result: ValidationRunResult) -> ValidationRunResult:
    output_dir = result.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    pass_fail = _pass_fail_matrix(result)
    all_metrics = _all_metrics(result)
    diagnostics = _failure_diagnostics(result)
    summary = _summary_json(result)
    paths = {
        "validation_summary_md": write_markdown(
            output_dir / "validation_summary.md",
            "R3 Validation Summary",
            _summary_markdown_lines(result, pass_fail),
        ),
        "validation_summary_json": write_json(output_dir / "validation_summary.json", summary),
        "pass_fail_matrix": write_csv(output_dir / "pass_fail_matrix.csv", pass_fail),
        "all_metrics": write_csv(output_dir / "all_metrics.csv", all_metrics),
        "failure_diagnostics": write_markdown(
            output_dir / "failure_diagnostics.md",
            "Failure Diagnostics",
            diagnostics,
        ),
    }
    result.artifacts.update(result_artifacts(paths))
    return result


def _pass_fail_matrix(result: ValidationRunResult) -> pd.DataFrame:
    rows = []
    for target_result in result.target_results:
        for level in target_result.level_results:
            rows.append(level.to_record())
    return pd.DataFrame(rows, columns=[
        "target", "level", "test_name", "key_metrics", "pass_fail", "failure_reason",
    ])


def _all_metrics(result: ValidationRunResult) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for target_result in result.target_results:
        for level in target_result.level_results:
            for key, value in level.key_metrics.items():
                rows.append({
                    "target": target_result.target,
                    "level": level.level,
                    "metric": key,
                    "value": value,
                })
    return pd.DataFrame(rows, columns=["target", "level", "metric", "value"])


def _failure_diagnostics(result: ValidationRunResult) -> list[str]:
    lines = [f"- Overall conclusion: `{result.conclusion}`"]
    notes = list(getattr(result, "notes", []))
    if notes:
        lines.append(f"- Notes: `{', '.join(notes)}`")
    lines.extend([
        "",
        "| Target | Level | Status | Failure Reason |",
        "|---|---|---|---|",
    ])
    failures = 0
    for target_result in result.target_results:
        for level in target_result.level_results:
            if level.passed:
                continue
            failures += 1
            lines.append(
                f"| {target_result.target} | {level.level} | {level.status} | {level.failure_reason or 'none'} |"
            )
    if failures == 0:
        lines.append("| all | all | PASS | none |")
    return lines


def _summary_json(result: ValidationRunResult) -> dict[str, Any]:
    return {
        "mode": result.mode,
        "targets": result.targets,
        "conclusion": result.conclusion,
        "notes": list(getattr(result, "notes", [])),
        "output_dir": str(result.output_dir),
        "targets_detail": [
            {
                "target": target_result.target,
                "validation_type": target_result.validation_type,
                "conclusion": target_result.conclusion,
                "notes": list(getattr(target_result, "notes", [])),
                "levels": [
                    {
                        "level": level.level,
                        "status": level.status,
                        "passed": level.passed,
                        "failure_reason": level.failure_reason,
                        "key_metrics": level.key_metrics,
                    }
                    for level in target_result.level_results
                ],
            }
            for target_result in result.target_results
        ],
    }


def _summary_markdown_lines(result: ValidationRunResult, pass_fail: pd.DataFrame) -> list[str]:
    lines = [
        f"- Mode: `{result.mode}`",
        f"- Conclusion: `{result.conclusion}`",
        f"- Targets: `{', '.join(result.targets)}`",
    ]
    notes = list(getattr(result, "notes", []))
    if notes:
        lines.append(f"- Notes: `{', '.join(notes)}`")
    lines.extend([
        "",
        "| Target | Level | Test Name | Key Metrics | Pass/Fail | Failure Reason |",
        "|---|---|---|---|---|---|",
    ])
    for _, row in pass_fail.iterrows():
        lines.append(
            f"| {row['target']} | {row['level']} | {row['test_name']} | "
            f"`{row['key_metrics']}` | {row['pass_fail']} | {row['failure_reason'] or 'none'} |"
        )
    return lines

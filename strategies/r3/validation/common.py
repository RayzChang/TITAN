"""Shared types for R3 Sprint 7 validation."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import json
import math

import numpy as np
import pandas as pd


TARGET_TREND = "trend_pullback_only"
TARGET_MEAN_REVERSION = "mean_reversion_only"
TARGET_FUNDING_REVERSAL = "funding_reversal_only"
TARGET_FULL_PORTFOLIO = "full_r3_portfolio"

VALIDATION_TARGETS = [
    TARGET_TREND,
    TARGET_MEAN_REVERSION,
    TARGET_FUNDING_REVERSAL,
    TARGET_FULL_PORTFOLIO,
]
SINGLE_STRATEGY_TARGETS = {
    TARGET_TREND: "trend_pullback",
    TARGET_MEAN_REVERSION: "mean_reversion",
    TARGET_FUNDING_REVERSAL: "funding_reversal",
}

VALIDATION_LEVELS = ["L0", "L1", "L2", "L3", "L4", "L5", "L6"]

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
STATUS_SKIPPED = "SKIPPED"

CONCLUSION_APPROVED = "APPROVED_FOR_DRY_RUN"
CONCLUSION_REJECTED = "REJECTED_NEEDS_REVISION"
CONCLUSION_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
CONCLUSION_SMOKE = "SMOKE_ONLY_NOT_FOR_DECISION"
ALLOWED_CONCLUSIONS = {
    CONCLUSION_APPROVED,
    CONCLUSION_REJECTED,
    CONCLUSION_INSUFFICIENT_DATA,
    CONCLUSION_SMOKE,
}

NOTE_DIAGNOSTIC_NOT_FOR_APPROVAL = "DIAGNOSTIC_MODE_NOT_FOR_APPROVAL"
NOTE_SINGLE_STRATEGY_DIAGNOSTIC = "SINGLE_STRATEGY_DIAGNOSTIC_ONLY"
NOTE_SINGLE_STRATEGY_CANNOT_APPROVE = "SINGLE_STRATEGY_CANNOT_APPROVE_DRY_RUN"


@dataclass
class ValidationContext:
    cfg: Any
    mode: str
    target: str
    symbols: list[str]
    initial_capital: float
    output_dir: Path
    simulations: int
    seed: int
    max_runtime_smoke: bool = False
    start: datetime | None = None
    end: datetime | None = None
    data_by_symbol: dict[str, dict[str, pd.DataFrame]] | None = None
    funding_by_symbol: dict[str, pd.DataFrame] | None = None
    premium_by_symbol: dict[str, pd.DataFrame] | None = None
    cache: dict[str, Any] = field(default_factory=dict)

    def child_output_dir(self, target: str, level: str) -> Path:
        path = self.output_dir / target / level
        path.mkdir(parents=True, exist_ok=True)
        return path


@dataclass(frozen=True)
class LevelResult:
    target: str
    level: str
    test_name: str
    status: str
    passed: bool
    key_metrics: dict[str, Any] = field(default_factory=dict)
    failure_reason: str = ""
    artifacts: dict[str, str] = field(default_factory=dict)
    data_warnings: list[str] = field(default_factory=list)

    def to_record(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "level": self.level,
            "test_name": self.test_name,
            "key_metrics": json.dumps(_json_safe(self.key_metrics), sort_keys=True),
            "pass_fail": self.status,
            "failure_reason": self.failure_reason,
        }


@dataclass
class TargetValidationResult:
    target: str
    validation_type: str
    mode: str
    level_results: list[LevelResult]
    conclusion: str
    notes: list[str] = field(default_factory=list)


@dataclass
class ValidationRunResult:
    mode: str
    targets: list[str]
    target_results: list[TargetValidationResult]
    conclusion: str
    output_dir: Path
    artifacts: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def target_validation_type(target: str) -> str:
    if target == TARGET_FULL_PORTFOLIO:
        return "full_portfolio_validation"
    return "single_strategy_diagnostic"


def is_terminal_failure(result: LevelResult) -> bool:
    return result.status in {STATUS_FAIL, STATUS_INSUFFICIENT_DATA}


def status_from_checks(
    checks: dict[str, bool],
    *,
    insufficient: bool = False,
) -> tuple[str, bool, str]:
    failed = [name for name, ok in checks.items() if not ok]
    if insufficient:
        return STATUS_INSUFFICIENT_DATA, False, "INSUFFICIENT_DATA"
    if failed:
        return STATUS_FAIL, False, "; ".join(failed)
    return STATUS_PASS, True, ""


def write_json(path: Path, data: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_json_safe(data), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path


def write_markdown(path: Path, title: str, lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = [f"# {title}", "", *lines]
    path.write_text("\n".join(body).rstrip() + "\n", encoding="utf-8")
    return path


def write_csv(path: Path, frame: pd.DataFrame) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False)
    return path


def result_artifacts(paths: dict[str, Path]) -> dict[str, str]:
    return {key: str(value) for key, value in paths.items()}


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        value = float(value)
    if isinstance(value, float):
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        if math.isnan(value):
            return None
        return value
    return value

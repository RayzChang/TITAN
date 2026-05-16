"""R3 Sprint 7 validation orchestrator."""
from __future__ import annotations

from pathlib import Path

from .common import (
    CONCLUSION_APPROVED,
    CONCLUSION_INSUFFICIENT_DATA,
    CONCLUSION_REJECTED,
    CONCLUSION_SMOKE,
    NOTE_DIAGNOSTIC_NOT_FOR_APPROVAL,
    NOTE_SINGLE_STRATEGY_CANNOT_APPROVE,
    NOTE_SINGLE_STRATEGY_DIAGNOSTIC,
    STATUS_INSUFFICIENT_DATA,
    TARGET_FULL_PORTFOLIO,
    VALIDATION_LEVELS,
    VALIDATION_TARGETS,
    LevelResult,
    TargetValidationResult,
    ValidationContext,
    ValidationRunResult,
    is_terminal_failure,
    target_validation_type,
)
from .l0_backtest import run_l0
from .l1_walk_forward import run_l1
from .l2_mcpt import run_l2
from .l3_bootstrap import run_l3
from .l4_bonferroni import run_l4
from .l5_oos import run_l5
from .l6_regime import run_l6
from .reporting import write_validation_reports


LEVEL_RUNNERS = {
    "L0": run_l0,
    "L1": run_l1,
    "L2": run_l2,
    "L3": run_l3,
    "L4": run_l4,
    "L5": run_l5,
    "L6": run_l6,
}


class R3Validator:
    """Run diagnostic or gated validation across R3 validation targets."""

    def __init__(self, cfg):
        self.cfg = cfg

    def run(
        self,
        *,
        mode: str,
        target: str,
        symbols: list[str],
        initial_capital: float,
        output_dir: str | Path,
        simulations: int,
        seed: int,
        max_runtime_smoke: bool = False,
        levels: list[str] | None = None,
        start=None,
        end=None,
        data_by_symbol=None,
        funding_by_symbol=None,
        premium_by_symbol=None,
    ) -> ValidationRunResult:
        mode = _normalize_mode(mode)
        targets = expand_targets(target)
        selected_levels = _normalize_levels(levels)
        output_path = Path(output_dir)
        target_results: list[TargetValidationResult] = []
        shared_cache = {}
        for one_target in targets:
            context = ValidationContext(
                cfg=self.cfg,
                mode=mode,
                target=one_target,
                symbols=symbols,
                initial_capital=float(initial_capital),
                output_dir=output_path,
                simulations=int(simulations),
                seed=int(seed),
                max_runtime_smoke=bool(max_runtime_smoke),
                start=start,
                end=end,
                data_by_symbol=data_by_symbol,
                funding_by_symbol=funding_by_symbol,
                premium_by_symbol=premium_by_symbol,
                cache=shared_cache,
            )
            target_results.append(self._run_target(context, one_target, selected_levels))
        run_result = ValidationRunResult(
            mode=mode,
            targets=targets,
            target_results=target_results,
            conclusion=self._overall_conclusion(mode, target_results, max_runtime_smoke),
            output_dir=output_path,
            notes=self._overall_notes(mode, target_results, max_runtime_smoke),
        )
        return write_validation_reports(run_result)

    def _run_target(
        self,
        context: ValidationContext,
        target: str,
        levels: list[str],
    ) -> TargetValidationResult:
        level_results: list[LevelResult] = []
        for level in levels:
            result = LEVEL_RUNNERS[level](context, target)
            level_results.append(result)
            if context.mode == "gated" and is_terminal_failure(result):
                break
        return TargetValidationResult(
            target=target,
            validation_type=target_validation_type(target),
            mode=context.mode,
            level_results=level_results,
            conclusion=self._target_conclusion(
                context.mode,
                target,
                level_results,
                context.max_runtime_smoke,
            ),
            notes=self._target_notes(context.mode, target, context.max_runtime_smoke),
        )

    def _target_conclusion(
        self,
        mode: str,
        target: str,
        level_results: list[LevelResult],
        max_runtime_smoke: bool,
    ) -> str:
        if max_runtime_smoke:
            return CONCLUSION_SMOKE
        if any(result.status == STATUS_INSUFFICIENT_DATA for result in level_results):
            return CONCLUSION_INSUFFICIENT_DATA
        if target != TARGET_FULL_PORTFOLIO:
            return CONCLUSION_REJECTED
        if mode != "gated":
            return CONCLUSION_REJECTED
        if any(result.data_warnings for result in level_results):
            return CONCLUSION_INSUFFICIENT_DATA
        observed_levels = [result.level for result in level_results]
        if (
            observed_levels == VALIDATION_LEVELS
            and all(result.passed for result in level_results)
        ):
            return CONCLUSION_APPROVED
        return CONCLUSION_REJECTED

    def _overall_conclusion(
        self,
        mode: str,
        target_results: list[TargetValidationResult],
        max_runtime_smoke: bool,
    ) -> str:
        if max_runtime_smoke:
            return CONCLUSION_SMOKE
        full = next((item for item in target_results if item.target == TARGET_FULL_PORTFOLIO), None)
        if mode != "gated":
            if any(item.conclusion == CONCLUSION_INSUFFICIENT_DATA for item in target_results):
                return CONCLUSION_INSUFFICIENT_DATA
            return CONCLUSION_REJECTED
        if full is None:
            if any(item.conclusion == CONCLUSION_INSUFFICIENT_DATA for item in target_results):
                return CONCLUSION_INSUFFICIENT_DATA
            return CONCLUSION_REJECTED
        if full.conclusion == CONCLUSION_APPROVED:
            if any(
                item.target != TARGET_FULL_PORTFOLIO
                and any(not level.passed for level in item.level_results)
                for item in target_results
            ):
                return CONCLUSION_REJECTED
            return CONCLUSION_APPROVED
        return full.conclusion

    def _target_notes(
        self,
        mode: str,
        target: str,
        max_runtime_smoke: bool,
    ) -> list[str]:
        notes: list[str] = []
        if max_runtime_smoke:
            notes.append(CONCLUSION_SMOKE)
        if mode == "diagnostic":
            notes.append(NOTE_DIAGNOSTIC_NOT_FOR_APPROVAL)
        if target != TARGET_FULL_PORTFOLIO:
            notes.extend([
                NOTE_SINGLE_STRATEGY_DIAGNOSTIC,
                NOTE_SINGLE_STRATEGY_CANNOT_APPROVE,
            ])
        return notes

    def _overall_notes(
        self,
        mode: str,
        target_results: list[TargetValidationResult],
        max_runtime_smoke: bool,
    ) -> list[str]:
        notes: list[str] = []
        if max_runtime_smoke:
            notes.append(CONCLUSION_SMOKE)
        if mode == "diagnostic":
            notes.append(NOTE_DIAGNOSTIC_NOT_FOR_APPROVAL)
        if all(item.target != TARGET_FULL_PORTFOLIO for item in target_results):
            notes.extend([
                NOTE_SINGLE_STRATEGY_DIAGNOSTIC,
                NOTE_SINGLE_STRATEGY_CANNOT_APPROVE,
            ])
        return notes


def expand_targets(target: str) -> list[str]:
    if target == "all":
        return list(VALIDATION_TARGETS)
    if target not in VALIDATION_TARGETS:
        raise ValueError(f"Unsupported validation target: {target}")
    return [target]


def _normalize_mode(mode: str) -> str:
    if mode not in {"diagnostic", "gated"}:
        raise ValueError("mode must be diagnostic or gated")
    return mode


def _normalize_levels(levels: list[str] | None) -> list[str]:
    if levels is None:
        return list(VALIDATION_LEVELS)
    selected = list(levels)
    if selected == ["all"]:
        return list(VALIDATION_LEVELS)
    unsupported = [level for level in selected if level not in VALIDATION_LEVELS]
    if unsupported:
        raise ValueError(f"Unsupported validation levels: {', '.join(unsupported)}")
    return selected

"""R3 Sprint 5 Funding Reversal strategy module.

The strategy only returns signals, risk plans, stop/exit plans, and order
intents. It does not place orders, run validation, or mutate position state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from .config_loader import R3Config
from .confirmation import ConfirmationResult, FundingReversalConfirmation5M
from .executor import FundingReversalOrderIntentBuilder, OrderIntent
from .indicators import atr, rsi, vwap_daily
from .regime import Direction, Regime, RegimeState
from .risk_engine import RiskEngine, RiskPlan
from .trailing import ExitPlan, FundingReversalStopExitBuilder, StopPlan


@dataclass(frozen=True)
class FundingReversalSignal:
    signal_id: str
    timestamp: datetime
    symbol: str
    strategy_name: str
    direction: str | None
    regime_state: RegimeState
    confirmation_result: ConfirmationResult | None
    risk_plan: RiskPlan | None
    entry_order_intent: OrderIntent | None
    stop_plan: StopPlan | None
    exit_plan: ExitPlan | None
    reason_codes: list[str] = field(default_factory=list)
    approved: bool = False
    rejection_reasons: list[str] = field(default_factory=list)
    metrics_snapshot: dict[str, Any] = field(default_factory=dict)


SignalEvaluationResult = FundingReversalSignal


class FundingReversalStrategy:
    strategy_name = "funding_reversal"

    def __init__(self, cfg: R3Config):
        self.cfg = cfg
        self.confirmation = FundingReversalConfirmation5M(cfg)
        self.risk_engine = RiskEngine(cfg)
        self.stop_exit_builder = FundingReversalStopExitBuilder(cfg)
        self.order_builder = FundingReversalOrderIntentBuilder(cfg)

    def evaluate(
        self,
        *,
        symbol: str,
        as_of: datetime,
        regime_state: RegimeState,
        df_1h: pd.DataFrame,
        df_5m: pd.DataFrame,
        equity: float,
        current_bid: float,
        current_ask: float,
        tick_size: float,
        current_open_risk_pct: float = 0.0,
        consecutive_losses: int = 0,
    ) -> FundingReversalSignal:
        signal_id = _signal_id(self.strategy_name, symbol, as_of)
        reason_codes: list[str] = []
        rejection_reasons: list[str] = []
        confirmation_result: ConfirmationResult | None = None
        risk_plan: RiskPlan | None = None
        stop_plan: StopPlan | None = None
        exit_plan: ExitPlan | None = None
        entry_order_intent: OrderIntent | None = None
        direction: str | None = None

        if not bool(self.cfg.funding_reversal.enabled):
            rejection_reasons.append("FUNDING_REVERSAL_DISABLED")
        if not _is_regime_c(regime_state):
            rejection_reasons.append("REGIME_NOT_C")
        if regime_state.direction not in {
            Direction.CONTRARIAN_LONG.value,
            Direction.CONTRARIAN_SHORT.value,
        }:
            rejection_reasons.append("REGIME_DIRECTION_NOT_CONTRARIAN")
        if not regime_state.allow_new_entries:
            rejection_reasons.append("ALLOW_NEW_ENTRIES_FALSE")

        metrics = dict(regime_state.metrics_snapshot)
        allow_extreme_vol = bool(self.cfg.funding_reversal.entry.allow_extreme_vol)
        if bool(metrics.get("extreme_vol", False)) and not allow_extreme_vol:
            rejection_reasons.append("EXTREME_VOL")

        df_1h_ready = ensure_funding_reversal_1h_columns(df_1h, self.cfg)
        df_1h_ready = _slice_until(df_1h_ready, as_of)
        if df_1h_ready.empty:
            rejection_reasons.append("NO_CLOSED_1H_BAR")
            return self._result(
                signal_id,
                as_of,
                symbol,
                direction,
                regime_state,
                confirmation_result,
                risk_plan,
                entry_order_intent,
                stop_plan,
                exit_plan,
                reason_codes,
                rejection_reasons,
                {"regime_metrics": metrics},
            )

        fr_setup = evaluate_funding_reversal_setup(
            df_1h_ready,
            self.cfg,
            funding_z=metrics.get("funding_z"),
            premium_z=metrics.get("premium_z"),
            regime_direction=regime_state.direction,
        )
        reason_codes.extend(fr_setup.reason_codes)
        if not fr_setup.passed:
            rejection_reasons.extend(fr_setup.rejection_reasons)
        direction = fr_setup.direction

        if direction is not None:
            confirmation_result = self.confirmation.check(df_5m, as_of, symbol, direction)
            reason_codes.extend(confirmation_result.reason_codes)
            if not confirmation_result.passed:
                rejection_reasons.append("FIVE_M_FR_CONFIRMATION_FAILED")

        if rejection_reasons:
            return self._result(
                signal_id,
                as_of,
                symbol,
                direction,
                regime_state,
                confirmation_result,
                risk_plan,
                entry_order_intent,
                stop_plan,
                exit_plan,
                reason_codes,
                rejection_reasons,
                {
                    "regime_metrics": metrics,
                    "fr_setup": fr_setup.metrics_snapshot,
                },
            )

        df_5m_ready = ensure_funding_reversal_5m_columns(df_5m, self.cfg)
        df_5m_ready = _slice_until(df_5m_ready, as_of)
        if df_5m_ready.empty:
            rejection_reasons.append("NO_CLOSED_5M_BAR")
            return self._result(
                signal_id,
                as_of,
                symbol,
                direction,
                regime_state,
                confirmation_result,
                risk_plan,
                entry_order_intent,
                stop_plan,
                exit_plan,
                reason_codes,
                rejection_reasons,
                {"regime_metrics": metrics},
            )

        atr_col = f"atr_{int(self.cfg.realized_vol.atr_period)}"
        signal_1h_row = df_1h_ready.iloc[-1]
        signal_5m_row = df_5m_ready.iloc[-1]
        signal_5m_close = float(signal_5m_row["close"])
        atr_1h = float(signal_1h_row[atr_col])

        entry_price = self.order_builder.compute_limit_price(
            direction=direction,
            current_bid=current_bid,
            current_ask=current_ask,
            tick_size=tick_size,
            signal_5m_close=signal_5m_close,
        )
        stop_plan = self.stop_exit_builder.build_stop_plan(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            atr_1h=atr_1h,
        )
        risk_multiplier = self.risk_engine.derive_risk_multiplier(consecutive_losses)
        risk_plan = self.risk_engine.build_plan(
            symbol=symbol,
            direction=direction,
            equity=equity,
            entry_price=entry_price,
            stop_price=stop_plan.stop_price,
            risk_multiplier=risk_multiplier,
            current_open_risk_pct=current_open_risk_pct,
            strategy_name=self.strategy_name,
        )
        if not risk_plan.approved:
            rejection_reasons.extend(risk_plan.rejection_reasons)
            return self._result(
                signal_id,
                as_of,
                symbol,
                direction,
                regime_state,
                confirmation_result,
                risk_plan,
                entry_order_intent,
                stop_plan,
                exit_plan,
                reason_codes,
                rejection_reasons,
                {"regime_metrics": metrics, "fr_setup": fr_setup.metrics_snapshot},
            )

        exit_plan = self.stop_exit_builder.build_exit_plan(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            stop_price=stop_plan.stop_price,
            vwap=float(signal_1h_row["vwap"]),
        )
        entry_order_intent = self.order_builder.build_entry_intent(
            symbol=symbol,
            direction=direction,
            signal_timestamp=as_of,
            current_bid=current_bid,
            current_ask=current_ask,
            tick_size=tick_size,
            signal_5m_close=signal_5m_close,
            quantity=risk_plan.quantity,
            signal_id=signal_id,
        )

        reason_codes.append("FUNDING_REVERSAL_APPROVED")
        return self._result(
            signal_id,
            as_of,
            symbol,
            direction,
            regime_state,
            confirmation_result,
            risk_plan,
            entry_order_intent,
            stop_plan,
            exit_plan,
            reason_codes,
            rejection_reasons,
            {
                "regime_metrics": metrics,
                "fr_setup": fr_setup.metrics_snapshot,
                "entry_price": entry_price,
            },
        )

    def _result(
        self,
        signal_id: str,
        timestamp: datetime,
        symbol: str,
        direction: str | None,
        regime_state: RegimeState,
        confirmation_result: ConfirmationResult | None,
        risk_plan: RiskPlan | None,
        entry_order_intent: OrderIntent | None,
        stop_plan: StopPlan | None,
        exit_plan: ExitPlan | None,
        reason_codes: list[str],
        rejection_reasons: list[str],
        metrics_snapshot: dict[str, Any],
    ) -> FundingReversalSignal:
        return FundingReversalSignal(
            signal_id=signal_id,
            timestamp=timestamp,
            symbol=symbol,
            strategy_name=self.strategy_name,
            direction=direction,
            regime_state=regime_state,
            confirmation_result=confirmation_result,
            risk_plan=risk_plan,
            entry_order_intent=entry_order_intent,
            stop_plan=stop_plan,
            exit_plan=exit_plan,
            reason_codes=reason_codes,
            approved=not rejection_reasons,
            rejection_reasons=rejection_reasons,
            metrics_snapshot=metrics_snapshot,
        )


@dataclass(frozen=True)
class FundingReversalSetupResult:
    passed: bool
    direction: str | None
    reason_codes: list[str] = field(default_factory=list)
    rejection_reasons: list[str] = field(default_factory=list)
    metrics_snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StructureResult:
    passed: bool
    direction: str
    reason_codes: list[str] = field(default_factory=list)
    rejection_reasons: list[str] = field(default_factory=list)
    metrics_snapshot: dict[str, Any] = field(default_factory=dict)


def evaluate_funding_reversal_setup(
    df_1h: pd.DataFrame,
    cfg: R3Config,
    *,
    funding_z: float | None,
    premium_z: float | None,
    regime_direction: str,
    as_of: datetime | None = None,
) -> FundingReversalSetupResult:
    df = ensure_funding_reversal_1h_columns(df_1h, cfg)
    if as_of is not None:
        df = _slice_until(df, as_of)
    if df.empty:
        return FundingReversalSetupResult(False, None, [], ["NO_CLOSED_1H_BAR"], {})

    if regime_direction == Direction.CONTRARIAN_SHORT.value:
        direction = "short"
    elif regime_direction == Direction.CONTRARIAN_LONG.value:
        direction = "long"
    else:
        return FundingReversalSetupResult(
            False,
            None,
            [],
            ["REGIME_DIRECTION_NOT_CONTRARIAN"],
            {"regime_direction": regime_direction},
        )

    row = df.iloc[-1]
    rsi_col = f"rsi_{int(cfg.funding_reversal.entry.rsi_period)}"
    required = ["high", "low", "close", "vwap", rsi_col]
    missing = [name for name in required if name not in df.columns or pd.isna(row[name])]
    if missing:
        return FundingReversalSetupResult(
            False,
            direction,
            [],
            ["MISSING_FR_INDICATORS"],
            {"missing_columns": missing},
        )
    if funding_z is None or premium_z is None:
        return FundingReversalSetupResult(
            False,
            direction,
            [],
            ["MISSING_FUNDING_PREMIUM_Z"],
            {"funding_z": funding_z, "premium_z": premium_z},
        )

    funding_value = float(funding_z)
    premium_value = float(premium_z)
    rsi_value = float(row[rsi_col])
    entry = cfg.funding_reversal.entry
    funding_threshold = float(entry.funding_z_threshold)
    premium_threshold = float(entry.premium_z_threshold)
    rsi_overbought = float(entry.rsi_overbought)
    rsi_oversold = float(entry.rsi_oversold)
    structure = evaluate_no_new_high_low(df, cfg, direction)
    metrics = {
        "funding_z": funding_value,
        "premium_z": premium_value,
        "funding_z_threshold": funding_threshold,
        "premium_z_threshold": premium_threshold,
        "rsi": rsi_value,
        "rsi_overbought": rsi_overbought,
        "rsi_oversold": rsi_oversold,
        **structure.metrics_snapshot,
    }

    rejection_reasons: list[str] = []
    reason_codes: list[str] = list(structure.reason_codes)
    if direction == "short":
        if funding_value < funding_threshold or premium_value < premium_threshold:
            rejection_reasons.append("FUNDING_PREMIUM_NOT_POSITIVE_EXTREME")
        if rsi_value <= rsi_overbought:
            rejection_reasons.append("RSI_NOT_OVERBOUGHT")
        if not structure.passed:
            rejection_reasons.extend(structure.rejection_reasons)
        if rejection_reasons:
            return FundingReversalSetupResult(False, direction, reason_codes, rejection_reasons, metrics)
        return FundingReversalSetupResult(True, direction, ["FR_SHORT_SETUP", *reason_codes], [], metrics)

    if funding_value > -funding_threshold or premium_value > -premium_threshold:
        rejection_reasons.append("FUNDING_PREMIUM_NOT_NEGATIVE_EXTREME")
    if rsi_value >= rsi_oversold:
        rejection_reasons.append("RSI_NOT_OVERSOLD")
    if not structure.passed:
        rejection_reasons.extend(structure.rejection_reasons)
    if rejection_reasons:
        return FundingReversalSetupResult(False, direction, reason_codes, rejection_reasons, metrics)
    return FundingReversalSetupResult(True, direction, ["FR_LONG_SETUP", *reason_codes], [], metrics)


def evaluate_no_new_high_low(
    df_1h: pd.DataFrame,
    cfg: R3Config,
    direction: str,
    as_of: datetime | None = None,
) -> StructureResult:
    if direction not in {"long", "short"}:
        return StructureResult(False, direction, [], ["INVALID_DIRECTION"], {})

    df = _slice_until(df_1h, as_of) if as_of is not None else df_1h
    lookback = int(cfg.funding_reversal.entry.no_new_high_low_lookback_bars)
    if len(df) < lookback + 1:
        return StructureResult(
            False,
            direction,
            [],
            ["INSUFFICIENT_STRUCTURE_DATA"],
            {"available_bars": len(df), "required_bars": lookback + 1},
        )

    if "high" not in df.columns or "low" not in df.columns:
        return StructureResult(False, direction, [], ["MISSING_STRUCTURE_COLUMNS"], {})

    current = df.iloc[-1]
    previous = df.iloc[-1 - lookback : -1]
    if direction == "short":
        current_high = float(current["high"])
        previous_high = float(previous["high"].max())
        passed = current_high <= previous_high
        return StructureResult(
            passed,
            direction,
            ["NO_NEW_HIGH_CONFIRMED"] if passed else [],
            [] if passed else ["NEW_HIGH_STILL_FORMING"],
            {
                "lookback_bars": lookback,
                "current_high": current_high,
                "previous_high_max": previous_high,
            },
        )

    current_low = float(current["low"])
    previous_low = float(previous["low"].min())
    passed = current_low >= previous_low
    return StructureResult(
        passed,
        direction,
        ["NO_NEW_LOW_CONFIRMED"] if passed else [],
        [] if passed else ["NEW_LOW_STILL_FORMING"],
        {
            "lookback_bars": lookback,
            "current_low": current_low,
            "previous_low_min": previous_low,
        },
    )


def ensure_funding_reversal_1h_columns(df_1h: pd.DataFrame, cfg: R3Config) -> pd.DataFrame:
    out = df_1h.copy()
    atr_period = int(cfg.realized_vol.atr_period)
    rsi_period = int(cfg.funding_reversal.entry.rsi_period)
    atr_col = f"atr_{atr_period}"
    rsi_col = f"rsi_{rsi_period}"

    if rsi_col not in out.columns:
        out[rsi_col] = rsi(out["close"], rsi_period)
    if atr_col not in out.columns:
        out[atr_col] = atr(out["high"], out["low"], out["close"], atr_period)
    if "vwap" not in out.columns:
        out["vwap"] = vwap_daily(out["high"], out["low"], out["close"], out["volume"])
    return out


def ensure_funding_reversal_5m_columns(df_5m: pd.DataFrame, cfg: R3Config) -> pd.DataFrame:
    out = df_5m.copy()
    atr_period = int(cfg.realized_vol.atr_period)
    atr_col = f"atr_{atr_period}"
    if atr_col not in out.columns:
        out[atr_col] = atr(out["high"], out["low"], out["close"], atr_period)
    return out


def _is_regime_c(regime_state: RegimeState) -> bool:
    return regime_state.regime == Regime.C_FUNDING_EXTREME or str(regime_state.regime) == "C"


def _slice_until(df: pd.DataFrame, as_of: datetime) -> pd.DataFrame:
    if not isinstance(df.index, pd.DatetimeIndex):
        return df
    ts = pd.Timestamp(as_of)
    if df.index.tz is not None and ts.tzinfo is None:
        ts = ts.tz_localize(df.index.tz)
    elif df.index.tz is None and ts.tzinfo is not None:
        ts = ts.tz_convert(None)
    return df.loc[df.index <= ts]


def _signal_id(strategy_name: str, symbol: str, as_of: datetime) -> str:
    safe_symbol = symbol.replace("/", "").replace(":", "")
    return f"{strategy_name}:{safe_symbol}:{pd.Timestamp(as_of).isoformat()}"

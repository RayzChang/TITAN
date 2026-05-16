"""R3 Sprint 4 Mean Reversion strategy module.

The strategy only returns signals, plans, and order intents. It does not place
orders, run validation, or mutate position state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from .config_loader import R3Config
from .confirmation import ConfirmationResult, MeanReversionConfirmation5M
from .executor import MeanReversionOrderIntentBuilder, OrderIntent
from .indicators import atr, bollinger_bands, rsi, vwap_daily, vwap_deviation_band
from .regime import Direction, Regime, RegimeState
from .risk_engine import RiskEngine, RiskPlan
from .trailing import ExitPlan, MeanReversionStopExitBuilder, StopPlan


@dataclass(frozen=True)
class MeanReversionSignal:
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


SignalEvaluationResult = MeanReversionSignal


class MeanReversionStrategy:
    strategy_name = "mean_reversion"

    def __init__(self, cfg: R3Config):
        self.cfg = cfg
        self.confirmation = MeanReversionConfirmation5M(cfg)
        self.risk_engine = RiskEngine(cfg)
        self.stop_exit_builder = MeanReversionStopExitBuilder(cfg)
        self.order_builder = MeanReversionOrderIntentBuilder(cfg)

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
    ) -> MeanReversionSignal:
        signal_id = _signal_id(self.strategy_name, symbol, as_of)
        reason_codes: list[str] = []
        rejection_reasons: list[str] = []
        confirmation_result: ConfirmationResult | None = None
        risk_plan: RiskPlan | None = None
        stop_plan: StopPlan | None = None
        exit_plan: ExitPlan | None = None
        entry_order_intent: OrderIntent | None = None
        direction: str | None = None

        if not bool(self.cfg.mean_reversion.enabled):
            rejection_reasons.append("MEAN_REVERSION_DISABLED")
        if not _is_regime_b(regime_state):
            rejection_reasons.append("REGIME_NOT_B")
        if regime_state.direction != Direction.NEUTRAL.value:
            rejection_reasons.append("REGIME_DIRECTION_NOT_NEUTRAL")
        if not regime_state.allow_new_entries:
            rejection_reasons.append("ALLOW_NEW_ENTRIES_FALSE")

        metrics = dict(regime_state.metrics_snapshot)
        funding_z = metrics.get("funding_z")
        funding_abs_max = float(self.cfg.mean_reversion.entry.funding_z_abs_max)
        if funding_z is None:
            rejection_reasons.append("MISSING_FUNDING_Z")
        elif abs(float(funding_z)) > funding_abs_max:
            rejection_reasons.append("FUNDING_NOT_NEUTRAL")
        if bool(metrics.get("extreme_vol", False)):
            rejection_reasons.append("EXTREME_VOL")

        df_1h_ready = ensure_mean_reversion_1h_columns(df_1h, self.cfg)
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

        mr_setup = evaluate_mean_reversion_setup(df_1h_ready, self.cfg)
        reason_codes.extend(mr_setup.reason_codes)
        if not mr_setup.passed:
            rejection_reasons.extend(mr_setup.rejection_reasons)
        direction = mr_setup.direction

        if direction is not None:
            confirmation_result = self.confirmation.check(df_5m, as_of, symbol, direction)
            reason_codes.extend(confirmation_result.reason_codes)
            if not confirmation_result.passed:
                rejection_reasons.append("FIVE_M_MR_CONFIRMATION_FAILED")

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
                    "mr_setup": mr_setup.metrics_snapshot,
                },
            )

        df_5m_ready = ensure_mean_reversion_5m_columns(df_5m, self.cfg)
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
                {"regime_metrics": metrics, "mr_setup": mr_setup.metrics_snapshot},
            )

        exit_plan = self.stop_exit_builder.build_exit_plan(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            stop_price=stop_plan.stop_price,
            bb_middle=float(signal_1h_row["bb_middle"]),
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

        reason_codes.append("MEAN_REVERSION_APPROVED")
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
                "mr_setup": mr_setup.metrics_snapshot,
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
    ) -> MeanReversionSignal:
        return MeanReversionSignal(
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
class MeanReversionSetupResult:
    passed: bool
    direction: str | None
    reason_codes: list[str] = field(default_factory=list)
    rejection_reasons: list[str] = field(default_factory=list)
    metrics_snapshot: dict[str, Any] = field(default_factory=dict)


def evaluate_mean_reversion_setup(
    df_1h: pd.DataFrame,
    cfg: R3Config,
    as_of: datetime | None = None,
) -> MeanReversionSetupResult:
    df = ensure_mean_reversion_1h_columns(df_1h, cfg)
    if as_of is not None:
        df = _slice_until(df, as_of)
    if df.empty:
        return MeanReversionSetupResult(False, None, [], ["NO_CLOSED_1H_BAR"], {})

    row = df.iloc[-1]
    required = ["close", "bb_lower", "bb_upper", "bb_middle", "vwap", "vwap_lower", "vwap_upper"]
    missing = [name for name in required if name not in df.columns or pd.isna(row[name])]
    rsi_col = f"rsi_{int(cfg.mean_reversion.entry.rsi_period)}"
    if rsi_col not in df.columns or pd.isna(row[rsi_col]):
        missing.append(rsi_col)
    if missing:
        return MeanReversionSetupResult(
            False,
            None,
            [],
            ["MISSING_MR_INDICATORS"],
            {"missing_columns": missing},
        )

    close = float(row["close"])
    bb_lower = float(row["bb_lower"])
    bb_upper = float(row["bb_upper"])
    vwap_lower = float(row["vwap_lower"])
    vwap_upper = float(row["vwap_upper"])
    rsi_value = float(row[rsi_col])
    long_rsi_max = float(cfg.mean_reversion.entry.rsi_long_max)
    short_rsi_min = float(cfg.mean_reversion.entry.rsi_short_min)

    long_conditions = [
        close < bb_lower,
        close < vwap_lower,
        rsi_value < long_rsi_max,
    ]
    short_conditions = [
        close > bb_upper,
        close > vwap_upper,
        rsi_value > short_rsi_min,
    ]
    metrics = {
        "close": close,
        "bb_lower": bb_lower,
        "bb_upper": bb_upper,
        "vwap_lower": vwap_lower,
        "vwap_upper": vwap_upper,
        "rsi": rsi_value,
        "rsi_long_max": long_rsi_max,
        "rsi_short_min": short_rsi_min,
    }
    if all(long_conditions):
        return MeanReversionSetupResult(True, "long", ["MR_LONG_SETUP"], [], metrics)
    if all(short_conditions):
        return MeanReversionSetupResult(True, "short", ["MR_SHORT_SETUP"], [], metrics)

    return MeanReversionSetupResult(False, None, [], ["NO_VALID_MR_SETUP"], metrics)


def ensure_mean_reversion_1h_columns(df_1h: pd.DataFrame, cfg: R3Config) -> pd.DataFrame:
    atr_period = int(cfg.realized_vol.atr_period)
    rsi_period = int(cfg.mean_reversion.entry.rsi_period)
    atr_col = f"atr_{atr_period}"
    rsi_col = f"rsi_{rsi_period}"
    required = {
        atr_col,
        rsi_col,
        "bb_upper",
        "bb_middle",
        "bb_lower",
        "vwap",
        "vwap_upper",
        "vwap_lower",
    }
    if required.issubset(df_1h.columns):
        return df_1h

    out = df_1h.copy()
    if rsi_col not in out.columns:
        out[rsi_col] = rsi(out["close"], rsi_period)
    if atr_col not in out.columns:
        out[atr_col] = atr(out["high"], out["low"], out["close"], atr_period)
    if "bb_upper" not in out.columns or "bb_middle" not in out.columns or "bb_lower" not in out.columns:
        bb = cfg.mean_reversion.bollinger
        bands = bollinger_bands(out["close"], int(bb.period), float(bb.std_multiplier))
        out["bb_upper"] = bands.upper
        out["bb_middle"] = bands.middle
        out["bb_lower"] = bands.lower
    if "vwap" not in out.columns:
        out["vwap"] = vwap_daily(out["high"], out["low"], out["close"], out["volume"])
    if "vwap_upper" not in out.columns or "vwap_lower" not in out.columns:
        dev = cfg.mean_reversion.vwap_deviation
        upper, lower, stdev = vwap_deviation_band(
            out["close"],
            out["vwap"],
            lookback_hours=int(dev.lookback_hours),
            multiplier=float(dev.multiplier),
            bars_per_hour=_bars_per_hour(str(cfg.timeframes.signal)),
        )
        out["vwap_upper"] = upper
        out["vwap_lower"] = lower
        out["vwap_stdev"] = stdev
    return out


def ensure_mean_reversion_5m_columns(df_5m: pd.DataFrame, cfg: R3Config) -> pd.DataFrame:
    atr_period = int(cfg.realized_vol.atr_period)
    atr_col = f"atr_{atr_period}"
    if atr_col in df_5m.columns:
        return df_5m

    out = df_5m.copy()
    if atr_col not in out.columns:
        out[atr_col] = atr(out["high"], out["low"], out["close"], atr_period)
    return out


def _is_regime_b(regime_state: RegimeState) -> bool:
    return regime_state.regime == Regime.B_SIDEWAYS or str(regime_state.regime) == "B"


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


def _bars_per_hour(timeframe: str) -> int:
    unit = timeframe[-1]
    value = int(timeframe[:-1])
    if unit == "h":
        return int(1 / value) if value < 1 else 1
    if unit == "m":
        return int(60 / value)
    raise ValueError(f"Unsupported signal timeframe: {timeframe}")

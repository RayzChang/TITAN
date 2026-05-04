"""R3 Sprint 3 Trend Pullback Continuation strategy module.

The strategy only returns signals, plans, and order intents. It does not place
orders, run a backtest, or mutate position state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from .config_loader import R3Config
from .confirmation import ConfirmationResult, TrendConfirmation5M
from .executor import OrderIntent, TrendOrderIntentBuilder
from .indicators import atr, ema, latest_confirmed_pivot, rsi
from .regime import Regime, RegimeState
from .risk_engine import RiskEngine, RiskPlan
from .trailing import ExitPlan, StopPlan, TrendStopExitBuilder


@dataclass(frozen=True)
class PullbackResult:
    passed: bool
    matched_ema: str | None
    reason_codes: list[str] = field(default_factory=list)
    metrics_snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RSIResult:
    passed: bool
    reason_codes: list[str] = field(default_factory=list)
    metrics_snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SignalWindowResult:
    passed: bool
    reason_codes: list[str] = field(default_factory=list)
    metrics_snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SignalEvaluationResult:
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


TrendSignal = SignalEvaluationResult


class TrendPullbackStrategy:
    strategy_name = "trend_pullback"

    def __init__(self, cfg: R3Config):
        self.cfg = cfg
        self.confirmation = TrendConfirmation5M(cfg)
        self.risk_engine = RiskEngine(cfg)
        self.stop_exit_builder = TrendStopExitBuilder(cfg)
        self.order_builder = TrendOrderIntentBuilder(cfg)

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
    ) -> SignalEvaluationResult:
        signal_id = _signal_id(self.strategy_name, symbol, as_of)
        direction = regime_state.direction if regime_state.direction in {"long", "short"} else None
        reason_codes: list[str] = []
        rejection_reasons: list[str] = []
        confirmation_result: ConfirmationResult | None = None
        risk_plan: RiskPlan | None = None
        stop_plan: StopPlan | None = None
        exit_plan: ExitPlan | None = None
        entry_order_intent: OrderIntent | None = None

        if not bool(self.cfg.trend_pullback.enabled):
            rejection_reasons.append("TREND_PULLBACK_DISABLED")
        if not _is_regime_a(regime_state):
            rejection_reasons.append("REGIME_NOT_A")
        if direction is None:
            rejection_reasons.append("REGIME_DIRECTION_NOT_TREND")
        if not regime_state.allow_new_entries:
            rejection_reasons.append("ALLOW_NEW_ENTRIES_FALSE")

        metrics = dict(regime_state.metrics_snapshot)
        trend_rejections = self._trend_filter_rejections(direction, metrics)
        rejection_reasons.extend(trend_rejections)

        funding_z = metrics.get("funding_z")
        if direction == "long" and funding_z is not None:
            if funding_z >= float(self.cfg.trend_pullback.entry.funding_z_long_max):
                rejection_reasons.append("FUNDING_OVERHEATED_LONG")
        if direction == "short" and funding_z is not None:
            if funding_z <= float(self.cfg.trend_pullback.entry.funding_z_short_min):
                rejection_reasons.append("FUNDING_OVERCOLD_SHORT")
        if bool(metrics.get("extreme_vol", False)):
            rejection_reasons.append("EXTREME_VOL")

        df_1h_ind = ensure_1h_signal_columns(df_1h, self.cfg)
        df_1h_ready = _slice_until(df_1h_ind, as_of)
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

        signal_1h_close = _timestamp_from_index(df_1h_ready.index[-1])
        window_result = evaluate_signal_window(signal_1h_close, as_of, self.cfg)
        reason_codes.extend(window_result.reason_codes)
        if not window_result.passed:
            rejection_reasons.append("SIGNAL_WINDOW_EXPIRED")

        if direction is not None:
            pullback = evaluate_pullback_zone(df_1h_ready, self.cfg, direction)
            reason_codes.extend(pullback.reason_codes)
            if not pullback.passed:
                rejection_reasons.append("NO_VALID_PULLBACK")

            rsi_result = evaluate_rsi_rebound(df_1h_ready, self.cfg, direction)
            reason_codes.extend(rsi_result.reason_codes)
            if not rsi_result.passed:
                rejection_reasons.append("RSI_CONDITION_FAILED")

            confirmation_result = self.confirmation.check(df_5m, as_of, symbol, direction)
            reason_codes.extend(confirmation_result.reason_codes)
            if not confirmation_result.passed:
                rejection_reasons.append("FIVE_M_CONFIRMATION_FAILED")
        else:
            pullback = None
            rsi_result = None

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
                    "window": window_result.metrics_snapshot,
                    "pullback": pullback.metrics_snapshot if pullback else {},
                    "rsi": rsi_result.metrics_snapshot if rsi_result else {},
                },
            )

        df_5m_ready = ensure_5m_execution_columns(df_5m, self.cfg)
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

        entry_cfg = self.cfg.trend_pullback.entry
        atr_period = int(self.cfg.realized_vol.atr_period)
        ema20_col = f"ema_{int(entry_cfg.ema_short_period)}"
        atr_1h_col = f"atr_{atr_period}"
        atr_5m_col = f"atr_{atr_period}"
        signal_1h_row = df_1h_ready.iloc[-1]
        signal_5m_row = df_5m_ready.iloc[-1]
        ema20_1h = float(signal_1h_row[ema20_col])
        atr_1h = float(signal_1h_row[atr_1h_col])
        atr_5m = float(signal_5m_row[atr_5m_col])
        signal_5m_close = float(signal_5m_row["close"])

        entry_price = self.order_builder.compute_limit_price(
            direction=direction,
            current_bid=current_bid,
            current_ask=current_ask,
            tick_size=tick_size,
            ema20_1h=ema20_1h,
            signal_5m_close=signal_5m_close,
            atr_5m=atr_5m,
        )

        latest_pivot_low, latest_pivot_high = latest_1h_pivots(df_1h_ready)
        stop_plan = self.stop_exit_builder.build_stop_plan(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            atr_1h=atr_1h,
            latest_pivot_low=latest_pivot_low,
            latest_pivot_high=latest_pivot_high,
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
                {"regime_metrics": metrics},
            )

        exit_plan = self.stop_exit_builder.build_exit_plan(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            stop_price=stop_plan.stop_price,
        )
        entry_order_intent = self.order_builder.build_entry_intent(
            symbol=symbol,
            direction=direction,
            signal_timestamp=as_of,
            current_bid=current_bid,
            current_ask=current_ask,
            tick_size=tick_size,
            ema20_1h=ema20_1h,
            signal_5m_close=signal_5m_close,
            atr_5m=atr_5m,
            quantity=risk_plan.quantity,
            signal_id=signal_id,
        )

        reason_codes.append("TREND_PULLBACK_APPROVED")
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
                "window": window_result.metrics_snapshot,
                "pullback": pullback.metrics_snapshot if pullback else {},
                "rsi": rsi_result.metrics_snapshot if rsi_result else {},
                "entry_price": entry_price,
            },
        )

    def _trend_filter_rejections(
        self,
        direction: str | None,
        metrics: dict[str, Any],
    ) -> list[str]:
        rejections: list[str] = []
        ema_4h_short = metrics.get("ema_4h_short")
        ema_4h_long = metrics.get("ema_4h_long")
        adx_4h = metrics.get("adx_4h")
        adx_min = float(self.cfg.regime.a_trend.adx_4h_min)

        if ema_4h_short is None or ema_4h_long is None or adx_4h is None:
            return ["MISSING_4H_TREND_METRICS"]
        if adx_4h <= adx_min:
            rejections.append("ADX_BELOW_TREND_THRESHOLD")
        if direction == "long" and not (ema_4h_short > ema_4h_long):
            rejections.append("EMA50_NOT_ABOVE_EMA200")
        if direction == "short" and not (ema_4h_short < ema_4h_long):
            rejections.append("EMA50_NOT_BELOW_EMA200")
        return rejections

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
    ) -> SignalEvaluationResult:
        return SignalEvaluationResult(
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


def evaluate_pullback_zone(
    df_1h: pd.DataFrame,
    cfg: R3Config,
    direction: str,
    as_of: datetime | None = None,
) -> PullbackResult:
    df = ensure_1h_signal_columns(df_1h, cfg)
    if as_of is not None:
        df = _slice_until(df, as_of)
    if df.empty:
        return PullbackResult(False, None, ["NO_VALID_PULLBACK"], {"reason": "empty_df"})

    entry_cfg = cfg.trend_pullback.entry
    atr_period = int(cfg.realized_vol.atr_period)
    atr_col = f"atr_{atr_period}"
    ema_columns = [
        (f"ema_{int(entry_cfg.ema_short_period)}", "PULLBACK_TO_EMA20"),
        (f"ema_{int(entry_cfg.ema_long_period)}", "PULLBACK_TO_EMA50"),
    ]
    row = df.iloc[-1]
    atr_1h = float(row[atr_col])
    close = float(row["close"])
    low = float(row["low"])
    high = float(row["high"])
    touch_mult = float(entry_cfg.ema_pullback_atr_mult)
    band_mult = float(entry_cfg.ema_band_atr_mult)

    metrics: dict[str, Any] = {
        "low": low,
        "high": high,
        "close": close,
        "atr_1h": atr_1h,
        "touch_mult": touch_mult,
        "band_mult": band_mult,
    }
    for ema_col, reason_code in ema_columns:
        ema_value = float(row[ema_col])
        lower = ema_value - band_mult * atr_1h
        upper = ema_value + band_mult * atr_1h
        close_in_band = lower <= close <= upper
        if direction == "long":
            touched = low <= ema_value + touch_mult * atr_1h
        elif direction == "short":
            touched = high >= ema_value - touch_mult * atr_1h
        else:
            return PullbackResult(False, None, ["INVALID_DIRECTION"], metrics)
        metrics[ema_col] = ema_value
        metrics[f"{ema_col}_band"] = (lower, upper)
        metrics[f"{ema_col}_touched"] = touched
        metrics[f"{ema_col}_close_in_band"] = close_in_band
        if touched and close_in_band:
            return PullbackResult(True, ema_col, [reason_code], metrics)

    return PullbackResult(False, None, ["NO_VALID_PULLBACK"], metrics)


def evaluate_rsi_rebound(
    df_1h: pd.DataFrame,
    cfg: R3Config,
    direction: str,
    as_of: datetime | None = None,
) -> RSIResult:
    df = ensure_1h_signal_columns(df_1h, cfg)
    if as_of is not None:
        df = _slice_until(df, as_of)

    entry_cfg = cfg.trend_pullback.entry
    lookback = int(entry_cfg.rsi_lookback_bars)
    threshold = float(entry_cfg.rsi_threshold)
    rsi_col = f"rsi_{int(entry_cfg.rsi_period)}"
    if len(df) < lookback:
        return RSIResult(
            False,
            ["RSI_CONDITION_FAILED"],
            {"available_bars": len(df), "required": lookback},
        )

    rsi_window = df[rsi_col].iloc[-lookback:]
    current = float(rsi_window.iloc[-1])
    previous = float(rsi_window.iloc[-2])
    if direction == "long":
        passed = float(rsi_window.min()) <= threshold and current > previous and current > threshold
        reason = "RSI_REBOUNDED_LONG" if passed else "RSI_CONDITION_FAILED"
    elif direction == "short":
        passed = float(rsi_window.max()) >= threshold and current < previous and current < threshold
        reason = "RSI_REJECTED_SHORT" if passed else "RSI_CONDITION_FAILED"
    else:
        return RSIResult(False, ["INVALID_DIRECTION"], {})

    return RSIResult(
        passed,
        [reason],
        {
            "rsi_values": [float(value) for value in rsi_window.tolist()],
            "current_rsi": current,
            "previous_rsi": previous,
            "threshold": threshold,
        },
    )


def evaluate_signal_window(
    signal_1h_close: datetime,
    current_5m_close: datetime,
    cfg: R3Config,
) -> SignalWindowResult:
    bar_delta = _timeframe_delta(str(cfg.timeframes.execution))
    window_bars = int(cfg.trend_pullback.signal_validity_window_5m_bars)
    elapsed = pd.Timestamp(current_5m_close) - pd.Timestamp(signal_1h_close)
    elapsed_seconds = elapsed.total_seconds()
    bar_seconds = bar_delta.total_seconds()
    elapsed_bars = int(elapsed_seconds // bar_seconds) if elapsed_seconds >= 0 else -1
    passed = 0 < elapsed_bars <= window_bars
    reason = "SIGNAL_WINDOW_VALID" if passed else "SIGNAL_WINDOW_EXPIRED"
    return SignalWindowResult(
        passed,
        [reason],
        {
            "signal_1h_close": signal_1h_close,
            "current_5m_close": current_5m_close,
            "elapsed_bars": elapsed_bars,
            "window_bars": window_bars,
        },
    )


def ensure_1h_signal_columns(df_1h: pd.DataFrame, cfg: R3Config) -> pd.DataFrame:
    out = df_1h.copy()
    entry_cfg = cfg.trend_pullback.entry
    atr_period = int(cfg.realized_vol.atr_period)
    ema_short_col = f"ema_{int(entry_cfg.ema_short_period)}"
    ema_long_col = f"ema_{int(entry_cfg.ema_long_period)}"
    rsi_col = f"rsi_{int(entry_cfg.rsi_period)}"
    atr_col = f"atr_{atr_period}"

    if ema_short_col not in out.columns:
        out[ema_short_col] = ema(out["close"], int(entry_cfg.ema_short_period))
    if ema_long_col not in out.columns:
        out[ema_long_col] = ema(out["close"], int(entry_cfg.ema_long_period))
    if rsi_col not in out.columns:
        out[rsi_col] = rsi(out["close"], int(entry_cfg.rsi_period))
    if atr_col not in out.columns:
        out[atr_col] = atr(out["high"], out["low"], out["close"], atr_period)
    return out


def ensure_5m_execution_columns(df_5m: pd.DataFrame, cfg: R3Config) -> pd.DataFrame:
    out = df_5m.copy()
    atr_period = int(cfg.realized_vol.atr_period)
    atr_col = f"atr_{atr_period}"
    if atr_col not in out.columns:
        out[atr_col] = atr(out["high"], out["low"], out["close"], atr_period)
    return out


def latest_1h_pivots(df_1h: pd.DataFrame) -> tuple[float | None, float | None]:
    latest_low: float | None = None
    latest_high: float | None = None
    if "pivot_low_confirmed" in df_1h.columns:
        _, latest_low = latest_confirmed_pivot(df_1h["pivot_low_confirmed"])
    if "pivot_high_confirmed" in df_1h.columns:
        _, latest_high = latest_confirmed_pivot(df_1h["pivot_high_confirmed"])
    return latest_low, latest_high


def _is_regime_a(regime_state: RegimeState) -> bool:
    return regime_state.regime == Regime.A_TREND or str(regime_state.regime) == "A"


def _slice_until(df: pd.DataFrame, as_of: datetime) -> pd.DataFrame:
    if not isinstance(df.index, pd.DatetimeIndex):
        return df
    ts = pd.Timestamp(as_of)
    if df.index.tz is not None and ts.tzinfo is None:
        ts = ts.tz_localize(df.index.tz)
    elif df.index.tz is None and ts.tzinfo is not None:
        ts = ts.tz_convert(None)
    return df.loc[df.index <= ts]


def _timestamp_from_index(value: Any) -> datetime:
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime()
    return value


def _timeframe_delta(timeframe: str) -> timedelta:
    unit = timeframe[-1]
    value = int(timeframe[:-1])
    if unit == "m":
        return timedelta(minutes=value)
    if unit == "h":
        return timedelta(hours=value)
    if unit == "d":
        return timedelta(days=value)
    raise ValueError(f"Unsupported timeframe: {timeframe}")


def _signal_id(strategy_name: str, symbol: str, as_of: datetime) -> str:
    safe_symbol = symbol.replace("/", "").replace(":", "")
    return f"{strategy_name}:{safe_symbol}:{pd.Timestamp(as_of).isoformat()}"

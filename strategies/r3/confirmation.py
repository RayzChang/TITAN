"""R3 5m confirmation logic for data-only strategy signals."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from .config_loader import R3Config
from .indicators import (
    bearish_engulfing,
    bullish_engulfing,
    ema,
    hammer,
    rsi,
    shooting_star,
    strong_close,
    weak_close,
)


@dataclass(frozen=True)
class ConfirmationResult:
    timestamp: datetime
    symbol: str
    direction: str
    passed: bool
    conditions_passed: list[str] = field(default_factory=list)
    conditions_failed: list[str] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)
    metrics_snapshot: dict[str, Any] = field(default_factory=dict)
    strategy_name: str = "trend_pullback"


class TrendConfirmation5M:
    """Three-condition, two-of-three 5m confirmation for trend pullback only."""

    def __init__(self, cfg: R3Config):
        self.cfg = cfg
        c = cfg.trend_pullback.confirmation_5m
        self.ema9_period = int(c.ema9_period)
        self.slope_lookback = int(c.ema9_slope_lookback_bars)
        self.breakout_lookback = int(c.breakout_lookback)
        self.strong_close_pos_min = float(c.strong_close.close_position_min)
        self.strong_close_body_min = float(c.strong_close.body_ratio_min)
        self.engulfing_growth_min = float(c.engulfing.body_growth_min)

    def check(
        self,
        df_5m: pd.DataFrame,
        as_of: datetime,
        symbol: str,
        direction: str,
    ) -> ConfirmationResult:
        if direction not in {"long", "short"}:
            return ConfirmationResult(
                timestamp=as_of,
                symbol=symbol,
                direction=direction,
                passed=False,
                reason_codes=["INVALID_DIRECTION"],
            )
        return self._check_directional(df_5m, as_of, symbol, direction)

    def check_long(
        self,
        df_5m: pd.DataFrame,
        as_of: datetime,
        symbol: str,
    ) -> ConfirmationResult:
        return self.check(df_5m, as_of, symbol, "long")

    def check_short(
        self,
        df_5m: pd.DataFrame,
        as_of: datetime,
        symbol: str,
    ) -> ConfirmationResult:
        return self.check(df_5m, as_of, symbol, "short")

    def _check_directional(
        self,
        df_5m: pd.DataFrame,
        as_of: datetime,
        symbol: str,
        direction: str,
    ) -> ConfirmationResult:
        df = _slice_until(df_5m, as_of)
        required_columns = {"open", "high", "low", "close"}
        missing_columns = sorted(required_columns - set(df.columns))
        if missing_columns:
            return ConfirmationResult(
                timestamp=as_of,
                symbol=symbol,
                direction=direction,
                passed=False,
                reason_codes=["MISSING_5M_COLUMNS"],
                metrics_snapshot={"missing_columns": missing_columns},
            )

        min_required = max(
            self.ema9_period,
            self.breakout_lookback + 1,
            self.slope_lookback + 1,
            2,
        )
        if len(df) < min_required:
            return ConfirmationResult(
                timestamp=as_of,
                symbol=symbol,
                direction=direction,
                passed=False,
                reason_codes=["INSUFFICIENT_5M_BARS"],
                metrics_snapshot={"available_bars": len(df), "required": min_required},
            )

        ema9_col = f"ema_{self.ema9_period}"
        ema9 = df[ema9_col] if ema9_col in df.columns else ema(df["close"], self.ema9_period)
        last_close = float(df["close"].iloc[-1])
        last_open = float(df["open"].iloc[-1])
        last_high = float(df["high"].iloc[-1])
        last_low = float(df["low"].iloc[-1])
        last_ema9 = float(ema9.iloc[-1])
        slope_ref = float(ema9.iloc[-1 - self.slope_lookback])

        if direction == "long":
            cond1 = (last_close > last_ema9) and (last_ema9 > slope_ref)
            cond1_label = "C1_LONG_CLOSE_ABOVE_EMA9_AND_SLOPE_UP"
        else:
            cond1 = (last_close < last_ema9) and (last_ema9 < slope_ref)
            cond1_label = "C1_SHORT_CLOSE_BELOW_EMA9_AND_SLOPE_DOWN"

        prev_high = float(df["high"].iloc[-1 - self.breakout_lookback : -1].max())
        prev_low = float(df["low"].iloc[-1 - self.breakout_lookback : -1].min())
        if direction == "long":
            cond2 = last_close > prev_high
            cond2_label = "C2_LONG_CLOSE_BREAKS_PREV_HIGH"
        else:
            cond2 = last_close < prev_low
            cond2_label = "C2_SHORT_CLOSE_BREAKS_PREV_LOW"

        if direction == "long":
            engulf = _last_bullish_engulfing(df, self.engulfing_growth_min)
            close_pattern = _last_strong_close(
                last_open,
                last_high,
                last_low,
                last_close,
                self.strong_close_pos_min,
                self.strong_close_body_min,
            )
            cond3 = engulf or close_pattern
            cond3_label = "C3_LONG_ENGULFING_OR_STRONG_CLOSE"
            engulf_key = "bullish_engulfing"
            close_key = "strong_close"
        else:
            engulf = _last_bearish_engulfing(df, self.engulfing_growth_min)
            close_pattern = _last_weak_close(
                last_open,
                last_high,
                last_low,
                last_close,
                self.strong_close_pos_min,
                self.strong_close_body_min,
            )
            cond3 = engulf or close_pattern
            cond3_label = "C3_SHORT_ENGULFING_OR_WEAK_CLOSE"
            engulf_key = "bearish_engulfing"
            close_key = "weak_close"

        conditions = [
            (cond1, cond1_label),
            (cond2, cond2_label),
            (cond3, cond3_label),
        ]
        conditions_passed = [label for ok, label in conditions if ok]
        conditions_failed = [label for ok, label in conditions if not ok]
        passed_count = len(conditions_passed)
        passed = passed_count >= 2

        reason_codes = [
            f"PASSED_{passed_count}_OF_3",
            "TWO_OF_THREE_CONFIRMED" if passed else "LESS_THAN_TWO_OF_THREE",
            "LONG_CONFIRM_OK" if direction == "long" and passed else "",
            "SHORT_CONFIRM_OK" if direction == "short" and passed else "",
            "LONG_CONFIRM_FAIL" if direction == "long" and not passed else "",
            "SHORT_CONFIRM_FAIL" if direction == "short" and not passed else "",
        ]

        return ConfirmationResult(
            timestamp=as_of,
            symbol=symbol,
            direction=direction,
            passed=passed,
            conditions_passed=conditions_passed,
            conditions_failed=conditions_failed,
            reason_codes=[code for code in reason_codes if code],
            metrics_snapshot={
                "open": last_open,
                "high": last_high,
                "low": last_low,
                "close": last_close,
                "ema9": last_ema9,
                "ema9_slope_ref": slope_ref,
                "prev_high": prev_high,
                "prev_low": prev_low,
                engulf_key: engulf,
                close_key: close_pattern,
                "passed_count": passed_count,
            },
        )


class MeanReversionConfirmation5M:
    """Three-condition, two-of-three 5m exhaustion confirmation for MR only."""

    strategy_name = "mean_reversion"

    def __init__(self, cfg: R3Config):
        self.cfg = cfg
        c = cfg.mean_reversion.confirmation_5m
        self.close_position_min = float(c.close_position_in_range_min)
        self.rsi_period = int(cfg.mean_reversion.entry.rsi_period)
        self.rsi_oversold = float(c.rsi_oversold)
        self.rsi_overbought = float(c.rsi_overbought)
        patterns = c.patterns
        self.engulfing_growth_min = float(patterns.engulfing_body_growth_min)
        self.hammer_body_max_ratio = float(patterns.hammer_body_max_ratio)
        self.hammer_shadow_ratio_min = float(patterns.hammer_shadow_ratio_min)
        self.hammer_upper_shadow_max_ratio = float(patterns.hammer_upper_shadow_max_ratio)
        self.shooting_star_body_max_ratio = float(patterns.shooting_star_body_max_ratio)
        self.shooting_star_shadow_ratio_min = float(patterns.shooting_star_shadow_ratio_min)
        self.shooting_star_lower_shadow_max_ratio = float(
            patterns.shooting_star_lower_shadow_max_ratio
        )

    def check(
        self,
        df_5m: pd.DataFrame,
        as_of: datetime,
        symbol: str,
        direction: str,
    ) -> ConfirmationResult:
        if direction not in {"long", "short"}:
            return ConfirmationResult(
                timestamp=as_of,
                symbol=symbol,
                direction=direction,
                passed=False,
                reason_codes=["INVALID_DIRECTION"],
                strategy_name=self.strategy_name,
            )
        return self._check_directional(df_5m, as_of, symbol, direction)

    def check_long(
        self,
        df_5m: pd.DataFrame,
        as_of: datetime,
        symbol: str,
    ) -> ConfirmationResult:
        return self.check(df_5m, as_of, symbol, "long")

    def check_short(
        self,
        df_5m: pd.DataFrame,
        as_of: datetime,
        symbol: str,
    ) -> ConfirmationResult:
        return self.check(df_5m, as_of, symbol, "short")

    def _check_directional(
        self,
        df_5m: pd.DataFrame,
        as_of: datetime,
        symbol: str,
        direction: str,
    ) -> ConfirmationResult:
        df = _slice_until(df_5m, as_of)
        required_columns = {"open", "high", "low", "close"}
        missing_columns = sorted(required_columns - set(df.columns))
        if missing_columns:
            return ConfirmationResult(
                timestamp=as_of,
                symbol=symbol,
                direction=direction,
                passed=False,
                reason_codes=["MISSING_5M_COLUMNS"],
                metrics_snapshot={"missing_columns": missing_columns},
                strategy_name=self.strategy_name,
            )

        if len(df) < max(self.rsi_period + 1, 2):
            return ConfirmationResult(
                timestamp=as_of,
                symbol=symbol,
                direction=direction,
                passed=False,
                reason_codes=["INSUFFICIENT_5M_BARS"],
                metrics_snapshot={
                    "available_bars": len(df),
                    "required": max(self.rsi_period + 1, 2),
                },
                strategy_name=self.strategy_name,
            )

        rsi_col = f"rsi_{self.rsi_period}"
        rsi_values = df[rsi_col] if rsi_col in df.columns else rsi(df["close"], self.rsi_period)

        last = df.iloc[-1]
        prev_rsi = float(rsi_values.iloc[-2])
        current_rsi = float(rsi_values.iloc[-1])
        open_ = float(last["open"])
        high = float(last["high"])
        low = float(last["low"])
        close = float(last["close"])
        range_ = high - low

        if range_ <= 0:
            close_position = 0.0
            cond_close = False
        elif direction == "long":
            close_position = (close - low) / range_
            cond_close = close > open_ and close_position >= self.close_position_min
        else:
            close_position = (high - close) / range_
            cond_close = close < open_ and close_position >= self.close_position_min

        if direction == "long":
            engulf = _last_bullish_engulfing(df, self.engulfing_growth_min)
            candle_pattern = _last_hammer(
                open_,
                high,
                low,
                close,
                self.hammer_body_max_ratio,
                self.hammer_shadow_ratio_min,
                self.hammer_upper_shadow_max_ratio,
            )
            cond_pattern = engulf or candle_pattern
            cond_rsi = current_rsi < self.rsi_oversold and current_rsi > prev_rsi
            conditions = [
                (cond_close, "MR_BULLISH_CLOSE"),
                (cond_pattern, "MR_BULLISH_PATTERN"),
                (cond_rsi, "MR_RSI_REVERSAL_LONG"),
            ]
            pattern_metrics = {
                "bullish_engulfing": engulf,
                "hammer": candle_pattern,
            }
        else:
            engulf = _last_bearish_engulfing(df, self.engulfing_growth_min)
            candle_pattern = _last_shooting_star(
                open_,
                high,
                low,
                close,
                self.shooting_star_body_max_ratio,
                self.shooting_star_shadow_ratio_min,
                self.shooting_star_lower_shadow_max_ratio,
            )
            cond_pattern = engulf or candle_pattern
            cond_rsi = current_rsi > self.rsi_overbought and current_rsi < prev_rsi
            conditions = [
                (cond_close, "MR_BEARISH_CLOSE"),
                (cond_pattern, "MR_BEARISH_PATTERN"),
                (cond_rsi, "MR_RSI_REVERSAL_SHORT"),
            ]
            pattern_metrics = {
                "bearish_engulfing": engulf,
                "shooting_star": candle_pattern,
            }

        conditions_passed = [label for ok, label in conditions if ok]
        conditions_failed = [label for ok, label in conditions if not ok]
        passed_count = len(conditions_passed)
        passed = passed_count >= 2
        return ConfirmationResult(
            timestamp=as_of,
            symbol=symbol,
            direction=direction,
            passed=passed,
            conditions_passed=conditions_passed,
            conditions_failed=conditions_failed,
            reason_codes=[
                "MR_CONFIRMATION_PASSED" if passed else "MR_CONFIRMATION_FAILED",
                f"PASSED_{passed_count}_OF_3",
            ],
            metrics_snapshot={
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "close_position": float(close_position),
                "current_rsi": current_rsi,
                "previous_rsi": prev_rsi,
                "passed_count": passed_count,
                **pattern_metrics,
            },
            strategy_name=self.strategy_name,
        )


class FundingReversalConfirmation5M:
    """Three-condition, two-of-three 5m reversal confirmation for funding extremes."""

    strategy_name = "funding_reversal"

    def __init__(self, cfg: R3Config):
        self.cfg = cfg
        c = cfg.funding_reversal.confirmation_5m
        self.close_position_min = float(c.close_position_in_range_min)
        self.rsi_period = int(cfg.funding_reversal.entry.rsi_period)
        self.rsi_oversold = float(c.rsi_oversold)
        self.rsi_overbought = float(c.rsi_overbought)
        patterns = c.patterns
        self.engulfing_growth_min = float(patterns.engulfing_body_growth_min)
        self.hammer_body_max_ratio = float(patterns.hammer_body_max_ratio)
        self.hammer_shadow_ratio_min = float(patterns.hammer_shadow_ratio_min)
        self.hammer_upper_shadow_max_ratio = float(patterns.hammer_upper_shadow_max_ratio)
        self.shooting_star_body_max_ratio = float(patterns.shooting_star_body_max_ratio)
        self.shooting_star_shadow_ratio_min = float(patterns.shooting_star_shadow_ratio_min)
        self.shooting_star_lower_shadow_max_ratio = float(
            patterns.shooting_star_lower_shadow_max_ratio
        )

    def check(
        self,
        df_5m: pd.DataFrame,
        as_of: datetime,
        symbol: str,
        direction: str,
    ) -> ConfirmationResult:
        if direction not in {"long", "short"}:
            return ConfirmationResult(
                timestamp=as_of,
                symbol=symbol,
                direction=direction,
                passed=False,
                reason_codes=["INVALID_DIRECTION"],
                strategy_name=self.strategy_name,
            )
        return self._check_directional(df_5m, as_of, symbol, direction)

    def check_long(
        self,
        df_5m: pd.DataFrame,
        as_of: datetime,
        symbol: str,
    ) -> ConfirmationResult:
        return self.check(df_5m, as_of, symbol, "long")

    def check_short(
        self,
        df_5m: pd.DataFrame,
        as_of: datetime,
        symbol: str,
    ) -> ConfirmationResult:
        return self.check(df_5m, as_of, symbol, "short")

    def _check_directional(
        self,
        df_5m: pd.DataFrame,
        as_of: datetime,
        symbol: str,
        direction: str,
    ) -> ConfirmationResult:
        df = _slice_until(df_5m, as_of)
        required_columns = {"open", "high", "low", "close"}
        missing_columns = sorted(required_columns - set(df.columns))
        if missing_columns:
            return ConfirmationResult(
                timestamp=as_of,
                symbol=symbol,
                direction=direction,
                passed=False,
                reason_codes=["MISSING_5M_COLUMNS"],
                metrics_snapshot={"missing_columns": missing_columns},
                strategy_name=self.strategy_name,
            )

        if len(df) < max(self.rsi_period + 1, 2):
            return ConfirmationResult(
                timestamp=as_of,
                symbol=symbol,
                direction=direction,
                passed=False,
                reason_codes=["INSUFFICIENT_5M_BARS"],
                metrics_snapshot={
                    "available_bars": len(df),
                    "required": max(self.rsi_period + 1, 2),
                },
                strategy_name=self.strategy_name,
            )

        rsi_col = f"rsi_{self.rsi_period}"
        rsi_values = df[rsi_col] if rsi_col in df.columns else rsi(df["close"], self.rsi_period)

        last = df.iloc[-1]
        prev_rsi = float(rsi_values.iloc[-2])
        current_rsi = float(rsi_values.iloc[-1])
        open_ = float(last["open"])
        high = float(last["high"])
        low = float(last["low"])
        close = float(last["close"])
        range_ = high - low

        if range_ <= 0:
            close_position = 0.0
            cond_close = False
        elif direction == "long":
            close_position = (close - low) / range_
            cond_close = close > open_ and close_position >= self.close_position_min
        else:
            close_position = (high - close) / range_
            cond_close = close < open_ and close_position >= self.close_position_min

        if direction == "long":
            engulf = _last_bullish_engulfing(df, self.engulfing_growth_min)
            candle_pattern = _last_hammer(
                open_,
                high,
                low,
                close,
                self.hammer_body_max_ratio,
                self.hammer_shadow_ratio_min,
                self.hammer_upper_shadow_max_ratio,
            )
            cond_pattern = engulf or candle_pattern
            cond_rsi = current_rsi < self.rsi_oversold and current_rsi > prev_rsi
            conditions = [
                (cond_close, "FR_BULLISH_CLOSE"),
                (cond_pattern, "FR_BULLISH_PATTERN"),
                (cond_rsi, "FR_RSI_REVERSAL_LONG"),
            ]
            pattern_metrics = {
                "bullish_engulfing": engulf,
                "hammer": candle_pattern,
            }
        else:
            engulf = _last_bearish_engulfing(df, self.engulfing_growth_min)
            candle_pattern = _last_shooting_star(
                open_,
                high,
                low,
                close,
                self.shooting_star_body_max_ratio,
                self.shooting_star_shadow_ratio_min,
                self.shooting_star_lower_shadow_max_ratio,
            )
            cond_pattern = engulf or candle_pattern
            cond_rsi = current_rsi > self.rsi_overbought and current_rsi < prev_rsi
            conditions = [
                (cond_close, "FR_BEARISH_CLOSE"),
                (cond_pattern, "FR_BEARISH_PATTERN"),
                (cond_rsi, "FR_RSI_REVERSAL_SHORT"),
            ]
            pattern_metrics = {
                "bearish_engulfing": engulf,
                "shooting_star": candle_pattern,
            }

        conditions_passed = [label for ok, label in conditions if ok]
        conditions_failed = [label for ok, label in conditions if not ok]
        passed_count = len(conditions_passed)
        passed = passed_count >= 2
        return ConfirmationResult(
            timestamp=as_of,
            symbol=symbol,
            direction=direction,
            passed=passed,
            conditions_passed=conditions_passed,
            conditions_failed=conditions_failed,
            reason_codes=[
                "FR_CONFIRMATION_PASSED" if passed else "FR_CONFIRMATION_FAILED",
                f"PASSED_{passed_count}_OF_3",
            ],
            metrics_snapshot={
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "close_position": float(close_position),
                "current_rsi": current_rsi,
                "previous_rsi": prev_rsi,
                "passed_count": passed_count,
                **pattern_metrics,
            },
            strategy_name=self.strategy_name,
        )


def _last_bullish_engulfing(df: pd.DataFrame, body_growth_min: float) -> bool:
    if len(df) < 2:
        return False
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    curr_body = abs(float(curr["close"]) - float(curr["open"]))
    prev_body = abs(float(prev["close"]) - float(prev["open"]))
    return (
        float(curr["close"]) > float(curr["open"])
        and float(curr["close"]) > float(prev["open"])
        and float(curr["open"]) < float(prev["close"])
        and curr_body > prev_body * body_growth_min
    )


def _last_bearish_engulfing(df: pd.DataFrame, body_growth_min: float) -> bool:
    if len(df) < 2:
        return False
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    curr_body = abs(float(curr["close"]) - float(curr["open"]))
    prev_body = abs(float(prev["close"]) - float(prev["open"]))
    return (
        float(curr["close"]) < float(curr["open"])
        and float(curr["close"]) < float(prev["open"])
        and float(curr["open"]) > float(prev["close"])
        and curr_body > prev_body * body_growth_min
    )


def _last_strong_close(
    open_: float,
    high: float,
    low: float,
    close: float,
    close_position_min: float,
    body_ratio_min: float,
) -> bool:
    range_ = high - low
    if range_ <= 0:
        return False
    return (
        close > open_
        and (close - low) / range_ >= close_position_min
        and abs(close - open_) / range_ >= body_ratio_min
    )


def _last_weak_close(
    open_: float,
    high: float,
    low: float,
    close: float,
    close_position_min: float,
    body_ratio_min: float,
) -> bool:
    range_ = high - low
    if range_ <= 0:
        return False
    return (
        close < open_
        and (high - close) / range_ >= close_position_min
        and abs(close - open_) / range_ >= body_ratio_min
    )


def _last_hammer(
    open_: float,
    high: float,
    low: float,
    close: float,
    body_max_ratio: float,
    lower_shadow_ratio_min: float,
    upper_shadow_max_ratio: float,
) -> bool:
    range_ = high - low
    if range_ <= 0:
        return False
    body = abs(close - open_)
    upper_shadow = high - max(close, open_)
    lower_shadow = min(close, open_) - low
    return (
        body / range_ <= body_max_ratio
        and lower_shadow >= upper_shadow * lower_shadow_ratio_min
        and upper_shadow / range_ <= upper_shadow_max_ratio
    )


def _last_shooting_star(
    open_: float,
    high: float,
    low: float,
    close: float,
    body_max_ratio: float,
    upper_shadow_ratio_min: float,
    lower_shadow_max_ratio: float,
) -> bool:
    range_ = high - low
    if range_ <= 0:
        return False
    body = abs(close - open_)
    upper_shadow = high - max(close, open_)
    lower_shadow = min(close, open_) - low
    return (
        body / range_ <= body_max_ratio
        and upper_shadow >= lower_shadow * upper_shadow_ratio_min
        and lower_shadow / range_ <= lower_shadow_max_ratio
    )


def _slice_until(df: pd.DataFrame, as_of: datetime) -> pd.DataFrame:
    if not isinstance(df.index, pd.DatetimeIndex):
        return df
    ts = pd.Timestamp(as_of)
    if df.index.tz is not None and ts.tzinfo is None:
        ts = ts.tz_localize(df.index.tz)
    elif df.index.tz is None and ts.tzinfo is not None:
        ts = ts.tz_convert(None)
    return df.loc[df.index <= ts]

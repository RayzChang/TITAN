"""
R3 Indicators
=============

Spec   : docs/R3_spec.md §3.5, §4, §5, §9, §11
Config : config/r3_strategy.yaml

工程紀律
--------
- 每個函數的參數**只接受配置物件 (R3Config) 或明確 kwargs**，不在函數內部 hardcode
- 純 pandas/numpy，避免 `ta` 套件的隱藏狀態與版本飄移
- 所有「依賴未來資料」的指標（pivot / rolling percentile）必須 `shift` 確保 in-sample causality
- 缺資料時回傳 NaN（**永不假造**），caller 自行判斷

實作範圍
--------
1. EMA                          (§4 trend pullback)
2. RSI                          (§4, §5)
3. ADX                          (§3.1, §3.2)
4. ATR / ATR_pct                (§3.5)
5. extreme_vol  (Q5 / Q13)      (§3.5)
6. Bollinger Bands              (§5, Q19)
7. VWAP daily                   (§5, Q19)
8. VWAP deviation zone          (§5, Q19)
9. funding_z                    (§9, Q3, Q4)
10. premium_z
11. confirmed_pivot             (§4.6, §7.2, Q9, Q14)
12. Candle patterns             (§4.2, §5.2, Q11, Q25)
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config_loader import R3Config


# =============================================================================
# 1. EMA
# =============================================================================
def ema(close: pd.Series, period: int) -> pd.Series:
    """指數移動平均（adjust=False，與 TradingView / 大多回測引擎一致）。"""
    if period <= 0:
        raise ValueError("EMA period must be positive")
    return close.ewm(span=period, adjust=False).mean()


# =============================================================================
# 2. RSI (Wilder's smoothing)
# =============================================================================
def rsi(close: pd.Series, period: int) -> pd.Series:
    """Wilder's RSI。前 `period` 根 NaN（無暖機假資料）。"""
    if period <= 0:
        raise ValueError("RSI period must be positive")
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    # 當 avg_loss == 0 且 avg_gain > 0 → RSI = 100
    out = out.where(~((avg_loss == 0) & (avg_gain > 0)), 100.0)
    # 當兩者皆 0 → 維持 NaN（資料不足）
    return out


# =============================================================================
# 3. ADX (Wilder)
# =============================================================================
def adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int,
) -> pd.Series:
    """
    Wilder ADX。回傳 ADX 序列（0~100）。

    步驟：
        TR  = max(H-L, |H-prevC|, |L-prevC|)
        +DM = max(currH - prevH, 0) when (currH - prevH) > (prevL - currL)
        -DM = max(prevL - currL, 0) when (prevL - currL) > (currH - prevH)
        +DI = 100 * Wilder(+DM) / Wilder(TR)
        -DI = 100 * Wilder(-DM) / Wilder(TR)
        DX  = 100 * |+DI - -DI| / (+DI + -DI)
        ADX = Wilder(DX, period)
    """
    if period <= 0:
        raise ValueError("ADX period must be positive")

    prev_close = close.shift(1)
    prev_high = high.shift(1)
    prev_low = low.shift(1)

    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    up_move = high - prev_high
    down_move = prev_low - low
    plus_dm = ((up_move > down_move) & (up_move > 0)).astype(float) * up_move.clip(lower=0.0)
    minus_dm = ((down_move > up_move) & (down_move > 0)).astype(float) * down_move.clip(lower=0.0)

    alpha = 1 / period
    atr_w = tr.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    plus_di = 100 * plus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean() / atr_w
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean() / atr_w

    di_sum = plus_di + minus_di
    dx = 100 * (plus_di - minus_di).abs() / di_sum.replace(0.0, np.nan)
    out = dx.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    return out


# =============================================================================
# 4. ATR / ATR_pct
# =============================================================================
def atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int,
) -> pd.Series:
    """Wilder's ATR。"""
    if period <= 0:
        raise ValueError("ATR period must be positive")
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def atr_pct(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int,
) -> pd.Series:
    """`ATR / close` — 標準化波動率，獨立於價位。"""
    return atr(high, low, close, period) / close


# =============================================================================
# 5. Extreme Vol (Q5 / Q13)
# =============================================================================
@dataclass(frozen=True)
class WarmupPolicy:
    """從 config.realized_vol.warmup 讀出的三段式策略。"""
    day_30_threshold_atr_pct: float          # Day 31~90 fallback 門檻
    rolling_lookback_days: int               # Day 91+ 滾動視窗
    rolling_percentile: int                  # 95
    day_30_trade_allowed: bool               # 通常 False
    day_31_to_90_trade_allowed: bool         # 通常 True


def warmup_policy_from_config(cfg: R3Config) -> WarmupPolicy:
    rv = cfg.realized_vol
    return WarmupPolicy(
        day_30_threshold_atr_pct=rv.warmup.day_31_to_90.atr_pct_extreme_threshold,
        rolling_lookback_days=rv.warmup.day_91_plus.lookback_days,
        rolling_percentile=rv.warmup.day_91_plus.threshold_percentile,
        day_30_trade_allowed=rv.warmup.day_1_to_30.trade_allowed,
        day_31_to_90_trade_allowed=rv.warmup.day_31_to_90.trade_allowed,
    )


def extreme_vol(
    atr_pct_series: pd.Series,
    bars_per_day: int,
    policy: WarmupPolicy,
) -> pd.Series:
    """
    輸出布林序列：True 表示「超過 extreme vol 門檻」。

    三段式（Q13）：
    - bar index 0 ~ 30*bars_per_day - 1     → False（Day 1~30 不交易，警報關）
    - 31*bars_per_day ~ 90*bars_per_day-1   → atr_pct > 0.04
    - 91*bars_per_day 起                    → atr_pct > rolling-90D percentile-95

    rolling percentile 用 **trailing** 視窗（含當根），但回傳當根的判定本身
    不需要未來資料，因此**不會 look-ahead**。
    """
    n = len(atr_pct_series)
    if n == 0:
        return pd.Series([], dtype=bool, index=atr_pct_series.index)

    day_30_end = 30 * bars_per_day
    day_90_end = 90 * bars_per_day
    rolling_window = policy.rolling_lookback_days * bars_per_day

    out = pd.Series(False, index=atr_pct_series.index)

    # 段 1: 0 ~ day_30_end-1 → False（不交易，警報無意義）
    # 段 2: day_30_end ~ day_90_end-1 → atr_pct > fixed threshold
    seg2 = atr_pct_series.iloc[day_30_end:day_90_end]
    out.iloc[day_30_end:day_90_end] = (seg2 > policy.day_30_threshold_atr_pct).values

    # 段 3: day_90_end 起 → rolling percentile
    if n > day_90_end:
        # 為了避免 look-ahead，rolling 視窗只看 i-rolling_window+1 ~ i 的資料
        rolling_pct = atr_pct_series.rolling(
            window=rolling_window, min_periods=rolling_window
        ).quantile(policy.rolling_percentile / 100.0)
        seg3_mask = atr_pct_series > rolling_pct
        out.iloc[day_90_end:] = seg3_mask.iloc[day_90_end:].fillna(False).values

    return out


def can_trade_on_warmup(
    bar_index: int,
    bars_per_day: int,
    policy: WarmupPolicy,
) -> bool:
    """純函式：第 bar_index 根 K 是否處於可交易期（Day 31+）。"""
    day_30_end = 30 * bars_per_day
    if bar_index < day_30_end:
        return policy.day_30_trade_allowed
    return policy.day_31_to_90_trade_allowed


# =============================================================================
# 6. Bollinger Bands
# =============================================================================
@dataclass(frozen=True)
class BollingerBands:
    upper: pd.Series
    middle: pd.Series
    lower: pd.Series
    width: pd.Series           # (upper - lower) / middle


def bollinger_bands(close: pd.Series, period: int, std_mult: float) -> BollingerBands:
    middle = close.rolling(window=period, min_periods=period).mean()
    std = close.rolling(window=period, min_periods=period).std(ddof=0)
    upper = middle + std_mult * std
    lower = middle - std_mult * std
    width = (upper - lower) / middle.replace(0.0, np.nan)
    return BollingerBands(upper=upper, middle=middle, lower=lower, width=width)


# =============================================================================
# 7. VWAP (daily reset, UTC 00:00)
# =============================================================================
def vwap_daily(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
) -> pd.Series:
    """
    Daily-reset VWAP（依 index 的 UTC 日期分組）。

    typical_price = (H + L + C) / 3
    VWAP_t = sum(tp * v) / sum(v)，每天 UTC 00:00 重置。

    要求 index 為 DatetimeIndex(UTC)。
    """
    if not isinstance(close.index, pd.DatetimeIndex):
        raise ValueError("VWAP requires DatetimeIndex")
    if close.index.tz is None:
        raise ValueError("VWAP requires UTC-aware DatetimeIndex")

    tp = (high + low + close) / 3.0
    pv = tp * volume

    day_key = close.index.tz_convert("UTC").date
    day_series = pd.Series(day_key, index=close.index)

    cum_pv = pv.groupby(day_series).cumsum()
    cum_v = volume.groupby(day_series).cumsum()
    return cum_pv / cum_v.replace(0.0, np.nan)


# =============================================================================
# 8. VWAP deviation zone
# =============================================================================
def vwap_deviation_band(
    close: pd.Series,
    vwap: pd.Series,
    lookback_hours: int,
    multiplier: float,
    bars_per_hour: int,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    回傳 (upper, lower, stdev_rolling)。

    stdev 採 (close - vwap) 的滾動 24h 標準差。
    """
    window = max(2, int(lookback_hours * bars_per_hour))
    diff = close - vwap
    stdev = diff.rolling(window=window, min_periods=window).std(ddof=0)
    upper = vwap + multiplier * stdev
    lower = vwap - multiplier * stdev
    return upper, lower, stdev


# =============================================================================
# 9. funding_z
# =============================================================================
def funding_z(
    funding_rate: pd.Series,
    lookback_days: int,
    funding_interval_hours: int,
    min_samples: int,
) -> pd.Series:
    """
    rolling z-score of funding rate (依 funding event 序列)。

    expected_samples = lookback_days * 24 / funding_interval_hours
    若視窗內有效樣本 < min_samples → 回傳 NaN（caller 應禁用 funding reversal）

    注意：funding_rate 的 index 是 funding **結算事件**時點，不是 K 線時點。
    """
    if funding_interval_hours <= 0:
        raise ValueError("funding_interval_hours must be positive")
    expected_samples = int(lookback_days * 24 / funding_interval_hours)
    window = max(min_samples, expected_samples)

    rolling_mean = funding_rate.rolling(window=window, min_periods=min_samples).mean()
    rolling_std = funding_rate.rolling(window=window, min_periods=min_samples).std(ddof=0)
    z = (funding_rate - rolling_mean) / rolling_std.replace(0.0, np.nan)
    return z


# =============================================================================
# 10. premium_z
# =============================================================================
def premium_z(
    premium_close: pd.Series,
    window: int,
    min_samples: int,
) -> pd.Series:
    """
    rolling z-score of premium index close。

    `premium_close` 來自 `R3ExchangeData.fetch_premium_index_klines(...)['close']`。
    回傳 NaN 若樣本不足。
    """
    if window <= 0 or min_samples <= 0:
        raise ValueError("window / min_samples must be positive")
    rolling_mean = premium_close.rolling(window=window, min_periods=min_samples).mean()
    rolling_std = premium_close.rolling(window=window, min_periods=min_samples).std(ddof=0)
    return (premium_close - rolling_mean) / rolling_std.replace(0.0, np.nan)


# =============================================================================
# 11. Confirmed Pivot (Q9 / Q14)
# =============================================================================
def pivot_high(high: pd.Series, n: int, confirm_delay_bars: int) -> pd.Series:
    """
    Pivot High 序列（confirmed only）。

    Pivot 條件：
        high[i] 嚴格大於 high[i-n .. i-1] 與 high[i+1 .. i+n]

    為避免 look-ahead：回傳序列在 index `i + confirm_delay_bars` 才標記 pivot。
    回傳 dtype=float：`high` 值 or NaN（非 pivot bar）。
    confirm_delay_bars 強制 ≥ n（至少要等右側 n 根 K 收盤）。
    """
    if n <= 0:
        raise ValueError("pivot n must be positive")
    if confirm_delay_bars < n:
        raise ValueError(f"confirm_delay_bars ({confirm_delay_bars}) must be >= n ({n})")

    rolling_max_left = high.rolling(window=n, min_periods=n).max().shift(1)
    rolling_max_right = high.shift(-n).rolling(window=n, min_periods=n).max()
    is_pivot_at_i = (high > rolling_max_left) & (high > rolling_max_right)

    # 標記在 confirmed bar 上（i + confirm_delay_bars）
    pivot_value = high.where(is_pivot_at_i)
    confirmed = pivot_value.shift(confirm_delay_bars)
    return confirmed


def pivot_low(low: pd.Series, n: int, confirm_delay_bars: int) -> pd.Series:
    """Pivot Low（confirmed only）。對稱版本。"""
    if n <= 0:
        raise ValueError("pivot n must be positive")
    if confirm_delay_bars < n:
        raise ValueError(f"confirm_delay_bars ({confirm_delay_bars}) must be >= n ({n})")

    rolling_min_left = low.rolling(window=n, min_periods=n).min().shift(1)
    rolling_min_right = low.shift(-n).rolling(window=n, min_periods=n).min()
    is_pivot_at_i = (low < rolling_min_left) & (low < rolling_min_right)

    pivot_value = low.where(is_pivot_at_i)
    confirmed = pivot_value.shift(confirm_delay_bars)
    return confirmed


def latest_confirmed_pivot(
    series: pd.Series,
    as_of_index: int | None = None,
) -> tuple[int | None, float | None]:
    """
    找出 `series` 在 `as_of_index` 之前最後一個 confirmed pivot。

    Returns (idx_int, value)，若不存在則 (None, None)。
    """
    if as_of_index is None:
        as_of_index = len(series) - 1
    if as_of_index < 0 or as_of_index >= len(series):
        return None, None
    seg = series.iloc[: as_of_index + 1].dropna()
    if seg.empty:
        return None, None
    last_idx = series.index.get_loc(seg.index[-1])
    return last_idx, float(seg.iloc[-1])


# =============================================================================
# 12. Candle Patterns
# =============================================================================
def strong_close(
    open_: pd.Series,
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    close_position_min: float,
    body_ratio_min: float,
) -> pd.Series:
    """
    Strong close (bullish):
        close > open
        (close - low) / (high - low) >= close_position_min
        |close - open| / (high - low) >= body_ratio_min
    """
    rng = (high - low).replace(0.0, np.nan)
    cond1 = close > open_
    cond2 = (close - low) / rng >= close_position_min
    cond3 = (close - open_).abs() / rng >= body_ratio_min
    return (cond1 & cond2 & cond3).fillna(False)


def weak_close(
    open_: pd.Series,
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    close_position_min: float,
    body_ratio_min: float,
) -> pd.Series:
    """Weak close (bearish)：對稱版本（close 接近 low）。"""
    rng = (high - low).replace(0.0, np.nan)
    cond1 = close < open_
    cond2 = (high - close) / rng >= close_position_min
    cond3 = (close - open_).abs() / rng >= body_ratio_min
    return (cond1 & cond2 & cond3).fillna(False)


def bullish_engulfing(
    open_: pd.Series,
    close: pd.Series,
    body_growth_min: float,
) -> pd.Series:
    """
    Bullish engulfing（Q11 規格）：
        current.close > previous.open
        current.open  < previous.close
        current.body  > previous.body × body_growth_min
        且當根本身為陽線
    """
    prev_open = open_.shift(1)
    prev_close = close.shift(1)
    curr_body = (close - open_).abs()
    prev_body = (prev_close - prev_open).abs()
    cond_curr_bull = close > open_
    cond_engulf_close = close > prev_open
    cond_engulf_open = open_ < prev_close
    cond_body = curr_body > prev_body * body_growth_min
    return (
        cond_curr_bull & cond_engulf_close & cond_engulf_open & cond_body
    ).fillna(False)


def bearish_engulfing(
    open_: pd.Series,
    close: pd.Series,
    body_growth_min: float,
) -> pd.Series:
    """Bearish engulfing — 對稱版本。"""
    prev_open = open_.shift(1)
    prev_close = close.shift(1)
    curr_body = (close - open_).abs()
    prev_body = (prev_close - prev_open).abs()
    cond_curr_bear = close < open_
    cond_engulf_close = close < prev_open
    cond_engulf_open = open_ > prev_close
    cond_body = curr_body > prev_body * body_growth_min
    return (
        cond_curr_bear & cond_engulf_close & cond_engulf_open & cond_body
    ).fillna(False)


def hammer(
    open_: pd.Series,
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    body_max_ratio: float = 0.3,
    lower_shadow_ratio_min: float = 2.0,
    upper_shadow_max_ratio: float = 0.3,
) -> pd.Series:
    """
    Hammer（探底 K）：
        body / range <= body_max_ratio
        lower_shadow >= upper_shadow * lower_shadow_ratio_min
        upper_shadow / range <= upper_shadow_max_ratio
    """
    rng = (high - low).replace(0.0, np.nan)
    body = (close - open_).abs()
    upper_shadow = high - close.where(close > open_, open_)
    lower_shadow = close.where(close < open_, open_) - low
    cond_small_body = body / rng <= body_max_ratio
    cond_long_lower = lower_shadow >= upper_shadow * lower_shadow_ratio_min
    cond_short_upper = upper_shadow / rng <= upper_shadow_max_ratio
    return (cond_small_body & cond_long_lower & cond_short_upper).fillna(False)


def shooting_star(
    open_: pd.Series,
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    body_max_ratio: float = 0.3,
    upper_shadow_ratio_min: float = 2.0,
    lower_shadow_max_ratio: float = 0.3,
) -> pd.Series:
    """Shooting star — Hammer 的對稱版本。"""
    rng = (high - low).replace(0.0, np.nan)
    body = (close - open_).abs()
    upper_shadow = high - close.where(close > open_, open_)
    lower_shadow = close.where(close < open_, open_) - low
    cond_small_body = body / rng <= body_max_ratio
    cond_long_upper = upper_shadow >= lower_shadow * upper_shadow_ratio_min
    cond_short_lower = lower_shadow / rng <= lower_shadow_max_ratio
    return (cond_small_body & cond_long_upper & cond_short_lower).fillna(False)


# =============================================================================
# Convenience: build a full indicator DF from config + ohlcv
# =============================================================================
def attach_core_indicators(
    df: pd.DataFrame,
    cfg: R3Config,
    timeframe: str,
) -> pd.DataFrame:
    """
    依 timeframe 把該時框需要的指標附到 OHLCV DF。

    這只是便利包裝；策略邏輯 (Sprint 3+) 會用更精細的選擇。

    Args:
        df: OHLCV DF
        cfg: R3Config
        timeframe: '1h' / '4h' / '5m'
    """
    if df.empty:
        return df.copy()

    out = df.copy()
    atr_period = cfg.realized_vol.atr_period

    if timeframe == "4h":
        # 4H 用於 Regime 判斷與大方向
        tfi = cfg.regime.trend_filter_indicators
        out[f"ema_{tfi.ema_short_period}"] = ema(out["close"], tfi.ema_short_period)
        out[f"ema_{tfi.ema_long_period}"] = ema(out["close"], tfi.ema_long_period)
        out[f"adx_{tfi.adx_period}"] = adx(
            out["high"], out["low"], out["close"], tfi.adx_period,
        )

    elif timeframe == "1h":
        tp = cfg.trend_pullback.entry
        out[f"ema_{tp.ema_short_period}"] = ema(out["close"], tp.ema_short_period)
        out[f"ema_{tp.ema_long_period}"] = ema(out["close"], tp.ema_long_period)
        out[f"rsi_{tp.rsi_period}"] = rsi(out["close"], tp.rsi_period)

        out[f"atr_{atr_period}"] = atr(
            out["high"], out["low"], out["close"], atr_period,
        )
        out[f"atr_pct_{atr_period}"] = out[f"atr_{atr_period}"] / out["close"]

        bb = cfg.mean_reversion.bollinger
        bands = bollinger_bands(out["close"], bb.period, bb.std_multiplier)
        out["bb_upper"] = bands.upper
        out["bb_middle"] = bands.middle
        out["bb_lower"] = bands.lower
        out["bb_width"] = bands.width

        # VWAP 與 deviation
        if (out["volume"] > 0).any():
            out["vwap"] = vwap_daily(out["high"], out["low"], out["close"], out["volume"])
            dev = cfg.mean_reversion.vwap_deviation
            upper, lower, stdev = vwap_deviation_band(
                out["close"], out["vwap"],
                lookback_hours=dev.lookback_hours,
                multiplier=dev.multiplier,
                bars_per_hour=1,                     # 1H bar
            )
            out["vwap_upper"] = upper
            out["vwap_lower"] = lower
            out["vwap_stdev"] = stdev

        # Pivot (1H, N=5, confirm 5)
        pcfg = cfg.pivot.normal
        out["pivot_high_confirmed"] = pivot_high(
            out["high"], pcfg.n, pcfg.confirm_delay_bars,
        )
        out["pivot_low_confirmed"] = pivot_low(
            out["low"], pcfg.n, pcfg.confirm_delay_bars,
        )

    elif timeframe == "5m":
        cf = cfg.trend_pullback.confirmation_5m
        out[f"ema_{cf.ema9_period}"] = ema(out["close"], cf.ema9_period)
        out[f"atr_{atr_period}"] = atr(
            out["high"], out["low"], out["close"], atr_period,
        )

        # Tight trailing pivot (5m, N=3, confirm 3)
        tt = cfg.pivot.tight_trailing
        out["pivot_high_5m_confirmed"] = pivot_high(
            out["high"], tt.n, tt.confirm_delay_bars,
        )
        out["pivot_low_5m_confirmed"] = pivot_low(
            out["low"], tt.n, tt.confirm_delay_bars,
        )

    return out

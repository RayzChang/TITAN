"""
技術指標模組 (Technical Indicators Module)

優先使用 ta 套件計算指標，若未安裝則 fallback 至純 pandas/numpy 實作。
安裝 ta 套件：pip install ta
"""
import pandas as pd
import numpy as np

# 嘗試匯入 ta 套件
try:
    import ta
    _TA_AVAILABLE = True
except ImportError:
    _TA_AVAILABLE = False


# ---------------------------------------------------------------------------
# EMA
# ---------------------------------------------------------------------------

def add_ema(df: pd.DataFrame, period: int, col_name: str = None) -> pd.DataFrame:
    """
    計算並加入 EMA（指數移動平均線）。

    Parameters
    ----------
    df       : OHLCV DataFrame（需含 close 欄位）
    period   : EMA 週期
    col_name : 輸出欄位名稱，預設 'ema_{period}'

    Returns
    -------
    原始 DataFrame 加上新欄位（in-place 修改後回傳）
    """
    if col_name is None:
        col_name = f'ema_{period}'

    if _TA_AVAILABLE:
        ema_indicator = ta.trend.EMAIndicator(close=df['close'], window=period, fillna=False)
        df[col_name] = ema_indicator.ema_indicator()
    else:
        # Fallback：pandas ewm，adjust=False 與標準 EMA 公式一致
        df[col_name] = df['close'].ewm(span=period, adjust=False).mean()

    return df


# ---------------------------------------------------------------------------
# RSI
# ---------------------------------------------------------------------------

def add_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """
    計算並加入 RSI（相對強弱指數）。

    Parameters
    ----------
    df     : OHLCV DataFrame（需含 close 欄位）
    period : RSI 週期，預設 14

    Returns
    -------
    原始 DataFrame 加上 'rsi_{period}' 欄位
    """
    col_name = f'rsi_{period}'

    if _TA_AVAILABLE:
        rsi_indicator = ta.momentum.RSIIndicator(close=df['close'], window=period, fillna=False)
        df[col_name] = rsi_indicator.rsi()
    else:
        # Fallback：Wilder's Smoothing（與 ta 套件邏輯相同）
        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)
        avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
        rs = avg_gain / avg_loss
        df[col_name] = 100 - (100 / (1 + rs))

    return df


# ---------------------------------------------------------------------------
# Bollinger Bands
# ---------------------------------------------------------------------------

def add_bollinger_bands(df: pd.DataFrame, period: int = 20, std_dev: float = 2.0) -> pd.DataFrame:
    """
    計算並加入 Bollinger Bands（布林通道）。

    新增欄位：
      bb_upper_{period}  — 上軌
      bb_middle_{period} — 中軌（SMA）
      bb_lower_{period}  — 下軌
      bb_width_{period}  — 通道寬度百分比 (width / middle * 100)
      bb_pct_{period}    — %B 指標

    Parameters
    ----------
    df      : OHLCV DataFrame（需含 close 欄位）
    period  : 布林通道週期，預設 20
    std_dev : 標準差倍數，預設 2.0

    Returns
    -------
    原始 DataFrame 加上新欄位
    """
    if _TA_AVAILABLE:
        bb = ta.volatility.BollingerBands(
            close=df['close'], window=period, window_dev=std_dev, fillna=False
        )
        df[f'bb_upper_{period}']  = bb.bollinger_hband()
        df[f'bb_middle_{period}'] = bb.bollinger_mavg()
        df[f'bb_lower_{period}']  = bb.bollinger_lband()
        df[f'bb_width_{period}']  = bb.bollinger_wband()
        df[f'bb_pct_{period}']    = bb.bollinger_pband()
    else:
        middle = df['close'].rolling(window=period).mean()
        std    = df['close'].rolling(window=period).std(ddof=0)
        upper  = middle + std_dev * std
        lower  = middle - std_dev * std
        df[f'bb_upper_{period}']  = upper
        df[f'bb_middle_{period}'] = middle
        df[f'bb_lower_{period}']  = lower
        df[f'bb_width_{period}']  = (upper - lower) / middle * 100
        df[f'bb_pct_{period}']    = (df['close'] - lower) / (upper - lower)

    return df


# ---------------------------------------------------------------------------
# MACD
# ---------------------------------------------------------------------------

def add_macd(df: pd.DataFrame) -> pd.DataFrame:
    """
    計算並加入 MACD（移動平均收斂散度）。
    使用標準參數：fast=12, slow=26, signal=9。

    新增欄位：
      macd        — MACD 線
      macd_signal — 訊號線
      macd_hist   — 柱狀圖（MACD - Signal）

    Parameters
    ----------
    df : OHLCV DataFrame（需含 close 欄位）

    Returns
    -------
    原始 DataFrame 加上新欄位
    """
    if _TA_AVAILABLE:
        macd_indicator = ta.trend.MACD(
            close=df['close'],
            window_slow=26,
            window_fast=12,
            window_sign=9,
            fillna=False
        )
        df['macd']        = macd_indicator.macd()
        df['macd_signal'] = macd_indicator.macd_signal()
        df['macd_hist']   = macd_indicator.macd_diff()
    else:
        ema_fast   = df['close'].ewm(span=12, adjust=False).mean()
        ema_slow   = df['close'].ewm(span=26, adjust=False).mean()
        macd_line  = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        df['macd']        = macd_line
        df['macd_signal'] = signal_line
        df['macd_hist']   = macd_line - signal_line

    return df


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------

def add_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """
    計算並加入 ATR（真實波幅均值）。

    Parameters
    ----------
    df     : OHLCV DataFrame（需含 high, low, close 欄位）
    period : ATR 週期，預設 14

    Returns
    -------
    原始 DataFrame 加上 'atr_{period}' 欄位
    """
    col_name = f'atr_{period}'

    if _TA_AVAILABLE:
        atr_indicator = ta.volatility.AverageTrueRange(
            high=df['high'], low=df['low'], close=df['close'],
            window=period, fillna=False
        )
        df[col_name] = atr_indicator.average_true_range()
    else:
        high_low   = df['high'] - df['low']
        high_close = (df['high'] - df['close'].shift(1)).abs()
        low_close  = (df['low']  - df['close'].shift(1)).abs()
        true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        # Wilder's smoothing（等同 alpha=1/period 的 ewm）
        df[col_name] = true_range.ewm(alpha=1 / period, adjust=False).mean()

    return df

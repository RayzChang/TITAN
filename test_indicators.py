"""
驗證腳本：技術指標 + EMA 交叉策略
執行方式：
    cd D:/02_trading
    .venv/Scripts/python test_indicators.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
import numpy as np
from indicators.technical import add_ema, add_rsi, add_bollinger_bands, add_macd, add_atr
from strategies.ema_crossover import EMAcrossover

print("=" * 60)
print("TITAN v1 — 技術指標模組驗證")
print("=" * 60)

# ---------------------------------------------------------------
# 建立假 OHLCV 數據（100 根 K 線）
# ---------------------------------------------------------------
np.random.seed(42)
dates = pd.date_range('2024-01-01', periods=100, freq='15min')
close = 50000 + np.cumsum(np.random.randn(100) * 100)
df = pd.DataFrame({
    'open':   close * 0.999,
    'high':   close * 1.002,
    'low':    close * 0.998,
    'close':  close,
    'volume': np.random.randint(100, 1000, 100).astype(float)
}, index=dates)

print(f"\n[DATA] K 線數量: {len(df)}, 收盤價範圍: {close.min():.2f} ~ {close.max():.2f}")

# ---------------------------------------------------------------
# 測試 EMA
# ---------------------------------------------------------------
df = add_ema(df, 9, 'ema_9')
df = add_ema(df, 21, 'ema_21')
print(f"\n[EMA]  ema_9  最後值: {df['ema_9'].iloc[-1]:.4f}")
print(f"[EMA]  ema_21 最後值: {df['ema_21'].iloc[-1]:.4f}")
assert df['ema_9'].notna().sum() > 0,  "EMA 9 計算失敗"
assert df['ema_21'].notna().sum() > 0, "EMA 21 計算失敗"

# ---------------------------------------------------------------
# 測試 RSI
# ---------------------------------------------------------------
df = add_rsi(df)
rsi_val = df['rsi_14'].iloc[-1]
print(f"\n[RSI]  rsi_14 最後值: {rsi_val:.4f}")
assert 0 <= rsi_val <= 100, f"RSI 值超出範圍: {rsi_val}"

# ---------------------------------------------------------------
# 測試 Bollinger Bands
# ---------------------------------------------------------------
df = add_bollinger_bands(df, period=20, std_dev=2.0)
print(f"\n[BB]   bb_upper_20  最後值: {df['bb_upper_20'].iloc[-1]:.4f}")
print(f"[BB]   bb_middle_20 最後值: {df['bb_middle_20'].iloc[-1]:.4f}")
print(f"[BB]   bb_lower_20  最後值: {df['bb_lower_20'].iloc[-1]:.4f}")
assert df['bb_upper_20'].iloc[-1] > df['bb_lower_20'].iloc[-1], "布林上軌應大於下軌"

# ---------------------------------------------------------------
# 測試 MACD
# ---------------------------------------------------------------
df = add_macd(df)
print(f"\n[MACD] macd        最後值: {df['macd'].iloc[-1]:.4f}")
print(f"[MACD] macd_signal 最後值: {df['macd_signal'].iloc[-1]:.4f}")
print(f"[MACD] macd_hist   最後值: {df['macd_hist'].iloc[-1]:.4f}")

# ---------------------------------------------------------------
# 測試 ATR
# ---------------------------------------------------------------
df = add_atr(df, period=14)
print(f"\n[ATR]  atr_14 最後值: {df['atr_14'].iloc[-1]:.4f}")
assert df['atr_14'].iloc[-1] > 0, "ATR 應大於 0"

# ---------------------------------------------------------------
# 測試策略
# ---------------------------------------------------------------
settings = {
    'strategy': {
        'ema_crossover': {
            'fast_period'   : 9,
            'slow_period'   : 21,
            'rsi_period'    : 14,
            'rsi_overbought': 70,
            'rsi_oversold'  : 30,
        }
    },
    'risk': {
        'stop_loss_pct'  : 1.5,
        'take_profit_pct': 3.0,
    }
}

strategy = EMAcrossover(settings)
print(f"\n[STRATEGY] {strategy}")

# 使用原始 df（策略內部會自己計算指標）
raw_df = df[['open', 'high', 'low', 'close', 'volume']].copy()
signal = strategy.calculate_signals(raw_df)
sl = strategy.get_stop_loss(50000, signal)
tp = strategy.get_take_profit(50000, signal)

print(f"[STRATEGY] 訊號:    {signal}")
print(f"[STRATEGY] 止損價:  {sl} (入場 50000, -{strategy.stop_loss_pct}%)")
print(f"[STRATEGY] 止盈價:  {tp} (入場 50000, +{strategy.take_profit_pct}%)")

assert signal in ('LONG', 'SHORT', 'HOLD'), f"非預期訊號: {signal}"
if signal == 'LONG':
    assert sl < 50000, "LONG 止損應低於入場價"
    assert tp > 50000, "LONG 止盈應高於入場價"
elif signal == 'SHORT':
    assert sl > 50000, "SHORT 止損應高於入場價"
    assert tp < 50000, "SHORT 止盈應低於入場價"

# ---------------------------------------------------------------
# 完整顯示最後 5 根 K 線的指標值
# ---------------------------------------------------------------
cols = ['close', 'ema_9', 'ema_21', 'rsi_14', 'macd', 'atr_14']
print(f"\n[TABLE] 最後 5 根 K 線指標值:")
print(df[cols].tail(5).to_string())

print("\n" + "=" * 60)
print("驗證通過!")
print("=" * 60)

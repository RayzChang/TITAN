"""
R3 Indicators
=============

Spec  : docs/R3_spec.md §3.5, §4, §5, §9
Config: config/r3_strategy.yaml

實作項目
--------
- atr_pct                    （Q5, §3.5）
- realized_vol_extreme       （Q13）
- ema_pullback_zone          （Q21）
- rsi_uptick_from_zone       （Q22）
- ema9_slope                 （Q20）
- bollinger_bands            （Q19）
- vwap_daily                 （Q19）
- vwap_deviation_band        （Q19）
- funding_z_score            （Q3, Q4）
- premium_z_score
- pivot_high_low_confirmed   （Q9, Q14）
- bullish_engulfing / bearish_engulfing
- hammer / shooting_star
- strong_close / weak_close
- close_in_upper_pct / close_in_lower_pct

每個函數的 spec 章節 + 對應 Q 編號都標在 docstring。
"""
from __future__ import annotations

# TODO[Sprint-1]: 實作以上函數。預計工時 0.5 天。
# 每個函數都要對 docs/R3_spec.md 的對應章節，並引用 config_loader 取參數。

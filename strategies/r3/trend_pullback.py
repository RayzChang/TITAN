"""
R3 主策略 1 — 趨勢回踩續行
==========================

Spec  : docs/R3_spec.md §4
Config: config/r3_strategy.yaml `trend_pullback`

進場條件（多單，全部成立）
--------------------------
1. Regime A，4H EMA50>200，ADX>22
2. 1H 回踩 EMA20/50 附近 (Q21)
3. 1H RSI 從 40-50 重新上彎 (Q22)
4. 5M 三條件二選二轉強 (Q11)
5. 1H 訊號在下一根 1H 期間內有效 (Q23)
6. funding_z < 2.0
"""
from __future__ import annotations


# TODO[Sprint-3]: 實作 TrendPullbackStrategy。預計工時 2 天。

"""
R3 主策略 2 — 均值回歸
======================

Spec  : docs/R3_spec.md §5
Config: config/r3_strategy.yaml `mean_reversion`

進場（多單）：
- Regime B
- 1H close < BB_lower(20, 2.0)
- 1H close < VWAP - 1.5×stdev_24h
- RSI(1H) < 28
- |funding_z| < 1.5
- 5M 止跌三條件二選二 (Q25)
"""
from __future__ import annotations


# TODO[Sprint-4]: 實作 MeanReversionStrategy。預計工時 1.5 天。

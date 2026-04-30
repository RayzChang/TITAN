"""
R3 Regime Classifier
====================

Spec  : docs/R3_spec.md §3
Config: config/r3_strategy.yaml `regime`

職責
----
- 每根 4H 收盤判斷 A/B 切換
- 每根 1H 收盤判斷 D1 觸發
- 即時事件判斷 D2 觸發
- 輸出 (current_regime, transition) — 供 router 決定路由策略
"""
from __future__ import annotations

from enum import Enum


class Regime(str, Enum):
    A_TREND = "A"
    B_RANGE = "B"
    C_EXTREME = "C"
    D1_MARKET = "D1"
    D2_SYSTEM = "D2"


# TODO[Sprint-2]: 實作 RegimeClassifier。預計工時 2 天。
# 必須參照 spec §3.1-3.6 的全部進入條件，從 config 取門檻值。

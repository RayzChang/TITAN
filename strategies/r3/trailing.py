"""
R3 Trailing Stop Modes
======================

Spec  : docs/R3_spec.md §7
Config: config/r3_strategy.yaml `trailing`

三種模式
--------
- standard         （§7.1）標準 trailing
- tight_trailing   （§7.2, Q1, Q14, Q16）A→B 切換後啟動，鎖死直到平倉
- emergency_tight  （§7.3, Q2）D1 觸發

Trigger：intrabar high/low hit + STOP_MARKET reduce-only（Tier 3.4）
"""
from __future__ import annotations


# TODO[Sprint-3]: 實作三種 trailing 計算函數。預計工時 1 天。
# 注意 Q16：tight_trailing 一旦啟動，restore_to_normal_trailing_if_back_to_a = false

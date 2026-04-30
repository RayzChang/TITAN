"""
R3 Executor — Maker first → Taker fallback
==========================================

Spec  : docs/R3_spec.md §4.4, §4.5
Config: trend_pullback.entry_order, mean_reversion.exit

職責
----
- Limit maker 掛單（pullback price + maker constraint）(Q12)
- Timeout 2 根 5M K 後 cancel
- 訊號仍有效 → 重新掛一次（不允許 chase）
- Taker fallback 條件檢查（4 條件全成立）
- 部分成交處理 (Q27)
"""
from __future__ import annotations


# TODO[Sprint-3]: 實作 R3Executor class。預計工時 3 天。
# 必須跟 core/order_manager.py 整合，重用 partial-fill / cancel 邏輯。

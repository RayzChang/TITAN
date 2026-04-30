"""
R3 5M Confirmation
==================

Spec  : docs/R3_spec.md §4.2 (trend), §5.2 (mean reversion)
Config: trend_pullback.confirmation_5m, mean_reversion.confirmation_5m

兩套獨立的「三條件二選二」邏輯：
- trend：包含「突破前 N 根 high」(順勢)
- mean_reversion：用「止跌/止漲」候選（反轉）

不要共用，避免 Q25 的混淆。
"""
from __future__ import annotations


# TODO[Sprint-3]: 實作 trend_long_confirmed / trend_short_confirmed
#                 + mr_long_confirmed / mr_short_confirmed
# 預計工時 0.5 天。Test 用 fixed bar fixture。

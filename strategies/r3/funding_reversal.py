"""
R3 副策略 — Funding / Premium 反轉
==================================

Spec  : docs/R3_spec.md §6
Config: config/r3_strategy.yaml `funding_reversal`

關鍵規則
--------
- 與既有反向倉位衝突時：等待 (Q18)
- 不再創高/破低判定：過去 5 根 1H 無新高/新低 (Tier 3.3)
- 副策略 risk cap 0.75%
"""
from __future__ import annotations


# TODO[Sprint-5]: 實作 FundingReversalStrategy。預計工時 1 天。

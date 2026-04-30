"""
R3 Risk Engine
==============

Spec  : docs/R3_spec.md §8
Config: config/r3_strategy.yaml `risk`

職責
----
- 倉位計算（Q26：用 limit_price）
- Equity 基準（Q28：保守算法）
- 連虧減半 / 恢復（Q6, Q7）
- BTC + ETH correlation haircut（Q8）
- Per-strategy & portfolio risk cap
- 反向倉位禁令（Q24）
"""
from __future__ import annotations


# TODO[Sprint-3]: 實作 RiskEngine class。預計工時 1.5 天。
# 對應的 unit tests 寫在 tests/test_r3.py 的 Q26/Q27/Q28 區塊。

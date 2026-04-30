"""
R3 Strategy Router
==================

Spec  : docs/R3_spec.md §0 整體架構
Config: regime.priority, risk.priority

職責
----
- 接收 RegimeClassifier 的當前 regime + transition
- 根據 regime 決定啟動哪個策略
- 處理多策略同時觸發時的優先級
- 處理 transition 帶來的特殊動作（Tight Trailing 啟動、Emergency Stop 啟動、cancel orders）
"""
from __future__ import annotations


# TODO[Sprint-4]: 實作 R3Router class。預計工時 2 天。
# 是整套 R3 的編排層，依 §0 架構圖實作。

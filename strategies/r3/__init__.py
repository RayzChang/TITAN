"""
R3 Crypto Futures Strategy
==========================

Spec   : docs/R3_spec.md (v1.0)
Config : config/r3_strategy.yaml
Lock   : 2026-04-30

模組組成：
    config_loader   — 載入 r3_strategy.yaml，提供 dataclass-style 存取
    regime          — Regime A/B/C/D 分類器
    indicators      — Bollinger / VWAP / Funding Z-score / Pivot detection
    confirmation    — 5M 三條件二選二（trend / mean_reversion 兩套）
    trend_pullback  — 主策略 1
    mean_reversion  — 主策略 2
    funding_reversal — 副策略
    risk_engine     — 倉位計算、連虧、correlation haircut、equity 基準
    trailing        — Standard / Tight / Emergency 三種 trailing 模式
    executor        — Limit maker → Taker fallback、partial fill 處理
    router          — Regime → Strategy 路由

實作順序見 Sprint 計劃。
"""

__version__ = "0.0.1-skeleton"
__spec_version__ = "1.0"
__spec_path__ = "docs/R3_spec.md"

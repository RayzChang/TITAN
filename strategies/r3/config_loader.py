"""
R3 Config Loader
================

載入 config/r3_strategy.yaml，提供型別安全的存取介面。

工程紀律：策略邏輯內**禁止 hardcode magic number**，
全部參數必須從這個 loader 取。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import yaml


_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "r3_strategy.yaml"


class R3Config:
    """
    薄包裝：提供 dict-like + dot-attr 存取。

    使用範例
    --------
    >>> cfg = R3Config.load()
    >>> cfg.regime.a_trend.adx_4h_min
    22
    >>> cfg.trend_pullback.entry.ema_pullback_atr_mult
    0.3
    """

    def __init__(self, data: Dict[str, Any]):
        self._data = data

    @classmethod
    def load(cls, path: str | Path | None = None) -> "R3Config":
        path = Path(path) if path else _DEFAULT_CONFIG_PATH
        if not path.exists():
            raise FileNotFoundError(f"R3 config not found: {path}")
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls(data)

    def __getattr__(self, key: str) -> Any:
        if key.startswith("_"):
            raise AttributeError(key)
        if key not in self._data:
            raise AttributeError(f"R3Config has no key: {key}")
        v = self._data[key]
        if isinstance(v, dict):
            return R3Config(v)
        return v

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def to_dict(self) -> Dict[str, Any]:
        return dict(self._data)

    def __repr__(self) -> str:
        return f"R3Config({list(self._data.keys())})"

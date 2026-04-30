"""
TITAN v1 — 設定檔載入器
負責載入 settings.yaml 並進行基本驗證
"""

import os
import yaml
from pathlib import Path


# 設定檔路徑
CONFIG_PATH = Path(__file__).parent / "settings.yaml"


def load_settings() -> dict:
    """載入並驗證設定檔，回傳設定字典"""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"找不到設定檔：{CONFIG_PATH}")

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        settings = yaml.safe_load(f)

    _validate(settings)
    return settings


def _validate(s: dict):
    """驗證關鍵參數，若有問題則拋出例外"""

    # 交易模式
    mode = s.get("mode", "")
    if mode not in ("testnet", "live"):
        raise ValueError(f"[設定錯誤] mode 必須是 testnet 或 live，目前是：{mode!r}")

    risk = s.get("risk", {})

    # 槓桿上限保護（Binance 永續合約最高 125x）
    leverage = risk.get("leverage", 1)
    if leverage > 125:
        raise ValueError(f"[設定錯誤] 槓桿不可超過 125x，目前設定為 {leverage}x")

    # 止損不可為零
    sl = risk.get("stop_loss_pct", 0)
    if sl <= 0:
        raise ValueError("[設定錯誤] 止損百分比必須大於 0，請設定止損！")

    # 倉位大小合理範圍
    pos_pct = risk.get("position_size_pct", 0)
    if not (1 <= pos_pct <= 50):
        raise ValueError(f"[設定錯誤] position_size_pct 建議在 1~50 之間，目前：{pos_pct}")

    # 正式模式額外警告
    if s.get("mode") == "live":
        print("=" * 60)
        print("⚠️  警告：目前為正式交易模式，將使用真實資金！")
        print(f"   槓桿：{leverage}x | 止損：{sl}% | 倉位：{pos_pct}%")
        print("=" * 60)

"""
TITAN v1 — 本地狀態持久化模組

職責：
  - 讀寫 state.json（持倉、箱體、防重複追單、下單冷卻）
  - 程式重啟時恢復狀態
  - 啟動時與交易所實際持倉比對，偵測孤兒倉位

設計原則：
  - 交易所才是最終真相，state.json 是「我們知道的事情」
  - 每次狀態變更後立即寫檔（避免程式崩潰遺失）
  - 原子寫入（先寫 .tmp 再 rename）避免寫到一半被中斷
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from utils.logger import get_logger

logger = get_logger()

STATE_PATH = Path(__file__).parent.parent / "state.json"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class StateStore:
    """本地狀態管理：持倉、箱體、防重複追單、下單冷卻。"""

    def __init__(self, path: Path = STATE_PATH):
        self.path = path
        self._state: dict = {
            "positions": {},       # symbol -> position info
            "pos_states": {},      # symbol -> {tp1_done, addon_done, frozen_sl_ceiling}
            "anti_repeat": {},     # symbol -> {needs_reset, signal_dir, ref_price}
            "last_order_ts": {},   # symbol -> iso timestamp (送單冷卻用)
            "last_updated": _utcnow_iso(),
        }
        self._load()

    def _load(self):
        if not self.path.exists():
            logger.info(f"[state] 無 state.json，從空狀態開始")
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._state.update(data)
            logger.info(f"[state] 載入 state.json：持倉 {len(self._state.get('positions', {}))} 筆")
        except Exception as e:
            logger.error(f"[state] 讀取 state.json 失敗：{e}，從空狀態開始")

    def _save(self):
        """原子寫入：先寫 tmp 再 rename"""
        self._state["last_updated"] = _utcnow_iso()
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=self.path.parent, prefix=".state_", suffix=".tmp"
            )
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.path)
        except Exception as e:
            logger.error(f"[state] 寫入 state.json 失敗：{e}")

    # ── 持倉狀態 ──────────────────────────────────────────────

    def record_position(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        amount: float,
        sl_price: float,
        tp_price: float,
    ):
        self._state["positions"][symbol] = {
            "side": side,
            "entry_price": entry_price,
            "amount": amount,
            "sl_price": sl_price,
            "tp_price": tp_price,
            "entry_time": _utcnow_iso(),
        }
        self._save()

    def update_position_amount(self, symbol: str, new_amount: float,
                               new_sl: float, new_tp: float):
        """加倉後更新數量與止損止盈"""
        if symbol in self._state["positions"]:
            self._state["positions"][symbol]["amount"]   = new_amount
            self._state["positions"][symbol]["sl_price"] = new_sl
            self._state["positions"][symbol]["tp_price"] = new_tp
            self._save()

    def clear_position(self, symbol: str):
        if symbol in self._state["positions"]:
            del self._state["positions"][symbol]
        if symbol in self._state.get("pos_states", {}):
            del self._state["pos_states"][symbol]
        self._save()

    # ── 持倉管理狀態（tp1_done, addon_done 等）──────────────────

    def save_pos_state(self, symbol: str, state: dict):
        """Q4：持久化 pos_state 到 state.json"""
        if "pos_states" not in self._state:
            self._state["pos_states"] = {}
        self._state["pos_states"][symbol] = state
        self._save()

    def get_pos_state(self, symbol: str) -> dict | None:
        return self._state.get("pos_states", {}).get(symbol)

    def all_pos_states(self) -> dict:
        return dict(self._state.get("pos_states", {}))

    # ── 防重複追單狀態 ────────────────────────────────────────────

    def save_anti_repeat(self, symbol: str, state: dict):
        """Fix 4：持久化 anti_repeat 到 state.json"""
        if "anti_repeat" not in self._state:
            self._state["anti_repeat"] = {}
        self._state["anti_repeat"][symbol] = state
        self._save()

    def get_anti_repeat(self, symbol: str) -> dict | None:
        return self._state.get("anti_repeat", {}).get(symbol)

    def all_anti_repeat(self) -> dict:
        return dict(self._state.get("anti_repeat", {}))

    def has_position(self, symbol: str) -> bool:
        return symbol in self._state["positions"]

    def get_position(self, symbol: str) -> Optional[dict]:
        return self._state["positions"].get(symbol)

    def all_positions(self) -> dict:
        return dict(self._state["positions"])

    # ── 下單冷卻 ──────────────────────────────────────────────

    def mark_order_sent(self, symbol: str):
        """記錄本次送單時間（用於下單冷卻檢查）"""
        self._state["last_order_ts"][symbol] = _utcnow_iso()
        self._save()

    def seconds_since_last_order(self, symbol: str) -> Optional[float]:
        ts = self._state.get("last_order_ts", {}).get(symbol)
        if not ts:
            return None
        try:
            last = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - last).total_seconds()
        except Exception:
            return None

"""
TITAN v1 — 風險管理模組（SHIELD 實作）
負責所有開倉前的風控檢查、倉位計算、狀態追蹤與異常偵測
所有狀態為 in-memory，重啟後重置為可接受行為
"""

import math
from datetime import datetime, timedelta
from typing import Optional

from utils.logger import get_logger

logger = get_logger()


class RiskManager:
    """
    TITAN 風控核心。所有交易決策必須經過此模組驗證。

    設計決策：
    - 狀態完全 in-memory，不依賴外部 DB，降低故障點
    - peak_balance 從初始餘額開始追蹤，永不下降
    - 連續虧損暫停計時以「最後一筆虧損的時間」為基準，而非暫停啟動時間
    - can_open_trade 是「AND 邏輯」：任一條件不滿足即拒絕
    """

    PAUSE_DURATION_HOURS = 1  # 連續虧損觸發的暫停時長

    def __init__(self, exchange, settings: dict):
        self.exchange = exchange
        self.risk_cfg = settings["risk"]
        self.capital_cfg = settings["capital"]

        # 從 settings 載入風控參數
        self.leverage: int = self.risk_cfg["leverage"]
        self.position_size_pct: float = self.risk_cfg["position_size_pct"]
        # 固定開倉金額（優先於百分比）
        self.position_fixed_usdt: float = self.capital_cfg.get("position_fixed_usdt", 0.0)
        self.max_open_positions: int = self.risk_cfg["max_open_positions"]
        self.max_daily_loss_pct: float = self.risk_cfg["max_daily_loss_pct"]
        self.max_daily_trades: int = self.risk_cfg["max_daily_trades"]
        self.consecutive_loss_pause: int = self.risk_cfg["consecutive_loss_pause"]
        self.drawdown_stop_pct: float = self.risk_cfg["drawdown_stop_pct"]
        self.anomaly_skip_pct: float = self.risk_cfg["anomaly_skip_pct"]
        self.total_usdt: float = self.capital_cfg["total_usdt"]

        # 日統計（每日重置）
        self.daily_pnl_usdt: float = 0.0
        self.trade_count_today: int = 0

        # 連續虧損追蹤
        self.consecutive_losses: int = 0
        self.last_loss_time: Optional[datetime] = None

        # 回撤追蹤：peak_balance 初始化為帳戶餘額（或設定資金，取較大值）
        try:
            current_balance = self.exchange.get_total_balance()
        except Exception:
            current_balance = self.total_usdt
        self.peak_balance: float = max(current_balance, self.total_usdt)

        # 暫停原因記錄（供報告使用）
        self._pause_reason: str = ""

        logger.info(
            f"[SHIELD] RiskManager 初始化完成 | "
            f"peak_balance={self.peak_balance:.2f} USDT | "
            f"leverage={self.leverage}x | "
            f"position_size={self.position_size_pct}%"
        )

    # ------------------------------------------------------------------
    # 1. 開倉許可檢查（AND 邏輯，全部通過才允許）
    # ------------------------------------------------------------------

    def can_open_trade(self, symbol: str) -> tuple[bool, str]:
        """
        檢查是否允許對指定 symbol 開新倉。
        回傳 (True/False, 原因說明)。
        """

        # ── 1. 連續虧損暫停（優先檢查，時間敏感）──
        if self.consecutive_losses >= self.consecutive_loss_pause:
            if self.last_loss_time is not None:
                pause_until = self.last_loss_time + timedelta(
                    hours=self.PAUSE_DURATION_HOURS
                )
                remaining = pause_until - datetime.now()
                if remaining.total_seconds() > 0:
                    mins = int(remaining.total_seconds() // 60)
                    reason = (
                        f"連續虧損 {self.consecutive_losses} 次，"
                        f"暫停交易中（剩餘 {mins} 分鐘）"
                    )
                    self._pause_reason = reason
                    logger.warning(f"[SHIELD] 拒絕開倉 [{symbol}]：{reason}")
                    return False, reason
                else:
                    # 暫停時間已過，自動解除
                    logger.info("[SHIELD] 連續虧損暫停已解除，恢復交易")
                    self.consecutive_losses = 0
                    self._pause_reason = ""

        # ── 2. 每日虧損上限 ──
        try:
            current_balance = self.exchange.get_total_balance()
        except Exception:
            current_balance = self.total_usdt

        daily_loss_pct = (
            abs(self.daily_pnl_usdt) / self.total_usdt * 100
            if self.daily_pnl_usdt < 0
            else 0.0
        )
        if daily_loss_pct >= self.max_daily_loss_pct:
            reason = (
                f"今日虧損 {daily_loss_pct:.2f}% 已達上限 "
                f"{self.max_daily_loss_pct}%"
            )
            self._pause_reason = reason
            logger.warning(f"[SHIELD] 拒絕開倉 [{symbol}]：{reason}")
            return False, reason

        # ── 3. 當前持倉數上限 ──
        try:
            open_positions = self.exchange.get_all_positions()
            active_count = len(
                [p for p in open_positions if float(p.get("contracts", 0)) != 0]
            )
        except Exception:
            active_count = 0

        if active_count >= self.max_open_positions:
            reason = (
                f"持倉數 {active_count} 已達上限 {self.max_open_positions}"
            )
            logger.warning(f"[SHIELD] 拒絕開倉 [{symbol}]：{reason}")
            return False, reason

        # ── 4. 今日交易次數上限 ──
        if self.trade_count_today >= self.max_daily_trades:
            reason = (
                f"今日交易次數 {self.trade_count_today} 已達上限 "
                f"{self.max_daily_trades}"
            )
            logger.warning(f"[SHIELD] 拒絕開倉 [{symbol}]：{reason}")
            return False, reason

        # ── 5. 餘額是否足夠開倉 ──
        required_margin = self.total_usdt * (self.position_size_pct / 100)
        if current_balance < required_margin:
            reason = (
                f"帳戶餘額 {current_balance:.2f} USDT 不足，"
                f"需要至少 {required_margin:.2f} USDT 保證金"
            )
            logger.warning(f"[SHIELD] 拒絕開倉 [{symbol}]：{reason}")
            return False, reason

        logger.info(f"[SHIELD] 允許開倉 [{symbol}]")
        return True, "風控通過"

    # ------------------------------------------------------------------
    # 2. 倉位計算
    # ------------------------------------------------------------------

    def calculate_position_size(self, balance: float, price: float) -> float:
        """
        計算下單數量（幣數）。

        若設定 capital.position_fixed_usdt > 0 → 使用固定保證金（如 100 USDT）
        否則 → 使用帳戶百分比計算

        公式：
            margin   = position_fixed_usdt  （固定模式）
                     = balance × position_size_pct / 100  （百分比模式）
            notional = margin × leverage
            amount   = notional / price  → floor 至 3 位小數
        """
        if self.position_fixed_usdt > 0:
            margin = self.position_fixed_usdt
        else:
            margin = balance * (self.position_size_pct / 100)

        notional   = margin * self.leverage
        raw_amount = notional / price
        amount     = math.floor(raw_amount * 1000) / 1000

        logger.debug(
            f"[SHIELD] 倉位計算 | margin={margin:.2f} USDT "
            f"price={price:.2f} notional={notional:.2f} amount={amount:.3f}"
        )
        return amount

    # ------------------------------------------------------------------
    # 3. 記錄已完成交易
    # ------------------------------------------------------------------

    def record_trade(self, pnl_usdt: float) -> None:
        """
        記錄一筆已完成交易的損益，更新所有風控狀態。
        此方法應於每筆訂單成交後（平倉後）呼叫。
        """
        self.daily_pnl_usdt += pnl_usdt
        self.trade_count_today += 1

        if pnl_usdt < 0:
            self.consecutive_losses += 1
            self.last_loss_time = datetime.now()
            logger.warning(
                f"[SHIELD] 虧損交易記錄 | pnl={pnl_usdt:.2f} USDT | "
                f"連續虧損={self.consecutive_losses} 次"
            )
        else:
            self.consecutive_losses = 0
            self._pause_reason = ""
            logger.info(
                f"[SHIELD] 獲利交易記錄 | pnl={pnl_usdt:.2f} USDT | "
                f"連續虧損計數已重置"
            )

        # 更新 peak_balance（複利模式下帳戶餘額可能上升）
        try:
            current_balance = self.exchange.get_total_balance()
            if current_balance > self.peak_balance:
                self.peak_balance = current_balance
                logger.info(
                    f"[SHIELD] 新高峰餘額：{self.peak_balance:.2f} USDT"
                )
        except Exception:
            pass

        logger.info(
            f"[SHIELD] 今日統計 | "
            f"pnl={self.daily_pnl_usdt:.2f} USDT | "
            f"交易次數={self.trade_count_today}"
        )

    # ------------------------------------------------------------------
    # 4. 回撤保護
    # ------------------------------------------------------------------

    def check_drawdown_stop(self, current_balance: float) -> bool:
        """
        檢查帳戶是否從峰值回撤超過 drawdown_stop_pct。
        回傳 True 表示需要停止交易。

        同時更新 peak_balance（如當前餘額超過歷史最高）。
        """
        if current_balance > self.peak_balance:
            self.peak_balance = current_balance

        if self.peak_balance <= 0:
            return False

        drawdown_pct = (
            (self.peak_balance - current_balance) / self.peak_balance * 100
        )

        if drawdown_pct >= self.drawdown_stop_pct:
            logger.error(
                f"[SHIELD] 回撤觸發停損！"
                f"peak={self.peak_balance:.2f} current={current_balance:.2f} "
                f"drawdown={drawdown_pct:.2f}% >= {self.drawdown_stop_pct}%"
            )
            return True

        return False

    # ------------------------------------------------------------------
    # 5. 異常行情偵測
    # ------------------------------------------------------------------

    def check_anomaly(self, df_last_candle) -> bool:
        """
        檢測最後一根 K 線是否為異常行情（波動過大）。
        回傳 True 表示異常，應跳過此根 K 線不進場。

        df_last_candle 可為 pd.Series 或含 high/low 欄位的 dict-like 物件。
        """
        high = float(df_last_candle["high"])
        low = float(df_last_candle["low"])

        if low <= 0:
            logger.warning("[SHIELD] 異常 K 線：low <= 0，視為異常")
            return True

        candle_range = abs(high - low) / low * 100

        if candle_range > self.anomaly_skip_pct:
            logger.warning(
                f"[SHIELD] 異常行情偵測！"
                f"high={high} low={low} range={candle_range:.2f}% "
                f"> {self.anomaly_skip_pct}%，跳過進場"
            )
            return True

        return False

    # ------------------------------------------------------------------
    # 6. 風控狀態報告
    # ------------------------------------------------------------------

    def get_risk_report(self) -> dict:
        """
        回傳當前完整風控狀態，供主程式、日誌或監控使用。
        """
        try:
            current_balance = self.exchange.get_total_balance()
        except Exception:
            current_balance = self.total_usdt

        daily_pnl_pct = self.daily_pnl_usdt / self.total_usdt * 100

        if self.peak_balance > 0:
            current_drawdown_pct = (
                (self.peak_balance - current_balance) / self.peak_balance * 100
            )
        else:
            current_drawdown_pct = 0.0

        # 判斷是否處於暫停狀態
        is_paused = False
        pause_reason = ""

        if self.consecutive_losses >= self.consecutive_loss_pause:
            if self.last_loss_time is not None:
                pause_until = self.last_loss_time + timedelta(
                    hours=self.PAUSE_DURATION_HOURS
                )
                if datetime.now() < pause_until:
                    is_paused = True
                    pause_reason = self._pause_reason

        if not is_paused and abs(daily_pnl_pct) >= self.max_daily_loss_pct and self.daily_pnl_usdt < 0:
            is_paused = True
            pause_reason = f"每日虧損上限 {self.max_daily_loss_pct}% 已觸發"

        return {
            "daily_pnl_usdt": round(self.daily_pnl_usdt, 4),
            "daily_pnl_pct": round(daily_pnl_pct, 4),
            "trade_count_today": self.trade_count_today,
            "consecutive_losses": self.consecutive_losses,
            "peak_balance": round(self.peak_balance, 4),
            "current_drawdown_pct": round(max(current_drawdown_pct, 0.0), 4),
            "is_paused": is_paused,
            "pause_reason": pause_reason,
        }

    # ------------------------------------------------------------------
    # 7. 每日重置
    # ------------------------------------------------------------------

    def reset_daily_stats(self) -> None:
        """
        重置所有日統計數據，應由排程器在每日午夜（UTC 00:00）呼叫。
        注意：連續虧損計數與 peak_balance 不重置（跨日連續性）。
        """
        logger.info(
            f"[SHIELD] 每日重置 | "
            f"前日 pnl={self.daily_pnl_usdt:.2f} USDT | "
            f"前日交易次數={self.trade_count_today}"
        )
        self.daily_pnl_usdt = 0.0
        self.trade_count_today = 0
        # 每日重置後，每日虧損暫停原因也一並清除
        if "每日虧損" in self._pause_reason:
            self._pause_reason = ""

"""
TITAN v1 Phase 3 — 主交易迴圈
Trading Intelligent Tactical Automation Network

啟動方式：
    python main.py

流程：
    1. 載入設定 + 連線交易所
    2. 初始化所有模組（策略、風控、倉位管理、掃描器）
    3. 設定槓桿 + 保證金模式
    4. APScheduler 啟動雙排程：
       - 每 N 秒掃描訊號（check_interval_seconds）
       - 每日 UTC 00:01 輸出日報告 + 重置統計
    5. Ctrl+C → 優雅關閉：強制平倉 → 輸出最終報告 → 退出
"""

import math
import sys
import signal
import time

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from datetime import datetime
from dotenv import load_dotenv

from config.settings_loader import load_settings
from core.exchange import Exchange
from core.position_manager import PositionManager
from core.risk_manager import RiskManager
from core.state_store import StateStore
from scanner.market_scanner import MarketScanner
from strategies.range_breakout import RangeBreakout
from utils.logger import get_logger

load_dotenv()
logger = get_logger()

# K 線根數設定（各時間週期）
KLINE_1H  = 200   # 1H  × 200 = 約 8 天，足夠 MACD 暖機
KLINE_4H  = 100   # 4H  × 100 = 約 17 天，TP1/加倉確認
KLINE_1D  = 120   # 1D  × 120 = 約 4 個月，箱體偵測用


# ══════════════════════════════════════════════════════════════════════
# TitanBot：主控核心
# ══════════════════════════════════════════════════════════════════════

class TitanBot:
    """
    TITAN 主控機器人。

    整合所有模組，驅動完整的自動化交易迴圈：
        掃描 K 線 → 計算訊號 → 風控許可 → 下單 → 監控平倉 → 每日報告
    """

    def __init__(self, settings: dict):
        self.settings  = settings
        self.mode      = settings.get('mode', 'testnet')
        self.timeframe = settings['strategy']['timeframe']
        self.interval  = settings['execution']['check_interval_seconds']
        self.compound  = settings['capital'].get('compound', True)

        # ── 初始化各模組 ──────────────────────────────────────────────
        logger.info("[TITAN] 初始化模組...")

        self.exchange = Exchange(settings)
        self.exchange.connect()

        self.strategy  = RangeBreakout(settings)
        self.scanner   = MarketScanner(self.exchange, settings)
        self.risk_mgr  = RiskManager(self.exchange, settings)
        self.pos_mgr   = PositionManager(self.exchange, settings)
        self.state     = StateStore()

        # 下單冷卻秒數（防止同 symbol 短時間重複送單）
        self.order_cooldown_sec: int = int(
            settings.get('execution', {}).get('order_cooldown_sec', 60)
        )
        # 市價單 timeout 秒數（送單失敗後查成交的等待上限）
        self.order_reconcile_sec: int = int(
            settings.get('execution', {}).get('order_reconcile_sec', 10)
        )

        # B：快取上次成功取得的餘額（fallback 用，不用初始值）
        try:
            self._last_known_balance: float = self.exchange.get_total_balance()
        except Exception:
            self._last_known_balance: float = float(
                settings['capital']['total_usdt']
            )
        self.initial_balance: float = self._last_known_balance

        # 排程器
        self.scheduler = BlockingScheduler(timezone='UTC')
        self._running  = False

        # 週期計數（用來控制狀態摘要顯示頻率）
        self._cycle_count: int = 0

        # 孤兒倉位清單（啟動對帳後填入，這些 symbol 禁止自動開新單）
        self._orphan_symbols: set = set()

        logger.info(f"[TITAN] 策略：{repr(self.strategy)}")
        logger.info(f"[TITAN] 初始餘額：${self.initial_balance:,.2f} USDT")
        logger.info(f"[TITAN] 複利模式：{'開啟（每次以最新餘額計算倉位）' if self.compound else '關閉（固定資金計算）'}")

    # ── 啟動 / 停止 ────────────────────────────────────────────────────

    def start(self):
        """啟動機器人：設定槓桿 → 排程 → 開始迴圈"""
        self._running = True

        # Unix 訊號處理（Ctrl+C / kill）
        signal.signal(signal.SIGINT,  self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        # 預先設定所有幣種的槓桿與保證金模式
        self._setup_symbols()

        # 啟動對帳：以交易所為真相，偵測孤兒倉位
        self._reconcile_on_startup()

        # 初始化每個 symbol 的箱體（手動 or 自動）
        self._init_boxes()

        # 立即執行一次初始掃描，確認系統正常
        logger.info("[TITAN] 執行初始掃描...")
        self.run_cycle()

        # 排程 1：每 N 秒掃描一次
        self.scheduler.add_job(
            self.run_cycle,
            trigger       = IntervalTrigger(seconds=self.interval),
            id            = 'strategy_cycle',
            max_instances = 1,    # 防止前一次未完成時重疊啟動
            coalesce      = True, # 若漏拍則只補跑一次
        )

        # 排程 2：每日 UTC 00:01 輸出日報告 + 重置統計
        self.scheduler.add_job(
            self._daily_report_and_reset,
            trigger = CronTrigger(hour=0, minute=1, timezone='UTC'),
            id      = 'daily_reset',
        )

        mode_label = "Demo Trading" if self.mode == "testnet" else "正式網 (真實資金！)"
        logger.info("=" * 60)
        logger.info("  TITAN v1 啟動完成，開始交易迴圈")
        logger.info(f"  模式：{mode_label}")
        logger.info(f"  K 線週期：{self.timeframe} | 掃描間隔：{self.interval}s")
        logger.info(f"  槓桿：{self.settings['risk']['leverage']}x | 保證金：全倉")
        logger.info("  按 Ctrl+C 安全停止機器人")
        logger.info("=" * 60)

        try:
            self.scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            self.stop()

    def stop(self):
        """優雅關閉：停止排程 → 強制平倉 → 輸出最終報告"""
        if not self._running:
            return
        self._running = False

        logger.info("")
        logger.info("=" * 60)
        logger.info("  [TITAN] 收到停止訊號，準備安全關閉...")
        logger.info("=" * 60)

        # 停止排程器
        try:
            if self.scheduler.running:
                self.scheduler.shutdown(wait=False)
        except Exception:
            pass

        # 強制平倉（若有持倉）
        active_count = self.pos_mgr.get_active_count()
        if active_count > 0:
            logger.info(f"[TITAN] 強制平倉 {active_count} 個倉位...")
            closed = self.pos_mgr.emergency_close_all()
            for trade in closed:
                self.risk_mgr.record_trade(trade.pnl_usdt)

        # 輸出最終報告
        self._print_final_report()
        logger.info("[TITAN] 安全關閉完成，再見！")

    # ── 主交易週期 ────────────────────────────────────────────────────

    def run_cycle(self):
        """
        一次完整的交易掃描週期：

        1. 同步倉位 → 偵測 SL/TP 觸發，通知風控記錄損益
        2. 帳戶回撤保護檢查 → 若觸發則停止所有交易
        3. 逐幣掃描訊號 → 符合條件且風控通過則開倉
        4. 定期輸出狀態摘要
        """
        if not self._running:
            return

        self._cycle_count += 1

        # ── Step 1：同步倉位，偵測已平倉 ──
        try:
            newly_closed = self.pos_mgr.sync_positions()
            for trade in newly_closed:
                self.risk_mgr.record_trade(trade.pnl_usdt)
                self.state.clear_position(trade.symbol)
                self.strategy.on_position_closed(trade.symbol)
                # R2：平倉後若箱體失效，立刻重建新箱體
                self.strategy.rebuild_box_if_invalidated(trade.symbol)
        except Exception as e:
            logger.warning(f"[TITAN] 倉位同步失敗：{e}")

        # 運行中孤兒偵測：每輪都對比 active_trades vs 交易所（保險 ②+③）
        try:
            live_positions = self.exchange.get_all_positions()
            live_syms = {p['symbol'] for p in live_positions}
            active_syms = set(self.pos_mgr.get_active_symbols())

            # 交易所有 + 本地沒 + 不在孤兒清單 → 新孤兒
            new_orphans = live_syms - active_syms - self._orphan_symbols
            for s in new_orphans:
                pos = next(p for p in live_positions if p['symbol'] == s)
                logger.warning(
                    f"[對帳] 運行中發現新孤兒倉位 {s} "
                    f"{pos.get('side')} {pos.get('contracts')} @ {pos.get('entryPrice')}，禁開新單"
                )
                self._orphan_symbols.add(s)

            # 原有孤兒消失（手動平倉） → 解除禁令
            cleared = {s for s in self._orphan_symbols if s not in live_syms}
            for s in cleared:
                logger.info(f"[對帳] {s} 孤兒倉位已消失（手動平倉），解除禁令")
                self._orphan_symbols.discard(s)
        except Exception as e:
            logger.debug(f"[對帳] 運行中對帳失敗：{e}")

        # ── Step 2：帳戶回撤保護 ──（B：快取上次成功值）
        try:
            current_balance = self.exchange.get_total_balance()
            self._last_known_balance = current_balance
        except Exception as e:
            logger.warning(f"[TITAN] 取得餘額失敗，使用快取值：{e}")
            current_balance = self._last_known_balance

        if self.risk_mgr.check_drawdown_stop(current_balance):
            logger.error("[TITAN] 帳戶回撤超過保護線，停止所有交易！")
            self.stop()
            return

        # ── Step 3：掃描訊號 ──
        try:
            symbols = self.scanner.get_tradeable_symbols()
        except Exception as e:
            logger.warning(f"[TITAN] 取得幣種列表失敗：{e}")
            return

        # ── Step 3a：持倉管理（TP1 / 減倉 / 加倉）──
        self._manage_open_positions()

        signals_found = 0
        for symbol in symbols:
            if not self._running:
                break
            if self._scan_symbol(symbol):
                signals_found += 1
            time.sleep(0.1)  # 避免觸發 rate limit

        # ── Step 4：週期狀態（每 10 個週期顯示一次）──
        if self._cycle_count % 10 == 0:
            self._print_cycle_status(current_balance, signals_found)

    # ── 單幣掃描 ──────────────────────────────────────────────────────

    def _scan_symbol(self, symbol: str) -> bool:
        """
        掃描單一幣種：取 K 線 → 計算訊號 → 風控 → 開倉。
        回傳 True 表示本次成功開倉。
        """
        # 已有持倉 → 跳過（等待交易所 SL/TP 自動觸發）
        if self.pos_mgr.is_in_position(symbol):
            return False

        # 取多時間週期 K 線
        try:
            df      = self.exchange.get_ohlcv(symbol, '1h',  limit=KLINE_1H)
            df_4h   = self.exchange.get_ohlcv(symbol, '4h',  limit=KLINE_4H)
            df_1d   = self.exchange.get_ohlcv(symbol, '1d',  limit=KLINE_1D)
        except Exception as e:
            logger.debug(f"[{symbol}] 取 K 線失敗：{e}")
            return False

        if len(df) < 40 or len(df_1d) < 30:
            logger.debug(f"[{symbol}] K 線不足，跳過")
            return False

        # 注入多時間週期資料至策略
        self.strategy.update_data(df_4h, df_1d)

        box_upper, box_lower = self.strategy.get_box(symbol)
        if box_upper and box_lower:
            box_detail = self.strategy.get_box_detail(symbol)
            ceilings   = box_detail.get('ceilings', [box_upper])
            ceil_str   = ' / '.join([str(c) for c in ceilings])
            logger.debug(
                f"[{symbol}] floor={box_lower:.2f} | "
                f"ceilings=[{ceil_str}] | 現價={df['close'].iloc[-1]:.2f}"
            )

        # 異常行情偵測（用已收完 1H K 棒）
        if self.risk_mgr.check_anomaly(df.iloc[-2]):
            return False

        # Fix 1：箱體狀態更新用「最近一根已收日線」（規格：箱體判斷用日線）
        last_daily = df_1d.iloc[-2]   # iloc[-1] 是今日未收完日線
        self.strategy.update_box(
            symbol         = symbol,
            current_high   = float(last_daily['high']),
            current_close  = float(last_daily['close']),
            current_volume = float(last_daily['volume']),
        )

        # 計算訊號
        try:
            trade_signal = self.strategy.calculate_signals(df, symbol)
        except Exception as e:
            logger.warning(f"[{symbol}] 策略計算失敗：{e}")
            return False

        # Fix 4：計算訊號後，把 anti_repeat 狀態寫回 state.json（防重複追單持久化）
        ar_state = self.strategy.get_anti_repeat_state(symbol)
        if ar_state:
            self.state.save_anti_repeat(symbol, ar_state)

        if trade_signal not in ('LONG', 'SHORT'):
            return False

        logger.info(f"[訊號] {symbol} >>> {trade_signal} <<<")

        # 風控許可
        ok, reason = self.risk_mgr.can_open_trade(symbol)
        if not ok:
            logger.info(f"[訊號] {symbol} 被風控攔截：{reason}")
            return False

        # Fix 5：把 df 傳進去，不重複 fetch（get_triggered_ceiling 用）
        return self._open_position(symbol, trade_signal, df_1h=df)

    # ── 開倉執行 ──────────────────────────────────────────────────────

    def _open_position(self, symbol: str, trade_signal: str,
                       df_1h=None) -> bool:
        """
        執行開倉（含全套防呆保護）：

        P0-1  下單前先檢查「交易所實際倉位」+「本地 state」，有倉即 skip
        P2-1  下單冷卻期檢查（60 秒內同 symbol 不可重複送單）
        P0-3  送單 exception 後 reconcile 實際成交狀態
        P1-1  成功後寫入 state.json
        """
        # ── P1-2：孤兒倉位禁開 ──
        if symbol in self._orphan_symbols:
            logger.warning(f"[開倉] {symbol} 為孤兒倉位（啟動時已存在、本地無紀錄），禁止開新單")
            return False

        # ── P0-1：下單前檢查 (交易所為真) ──
        try:
            live_pos = self.exchange.get_position(symbol)
            if live_pos and float(live_pos.get('contracts', 0)) != 0:
                logger.warning(
                    f"[開倉] {symbol} 交易所已有倉位 "
                    f"({live_pos.get('side')} {live_pos.get('contracts')})，跳過"
                )
                return False
        except Exception as e:
            logger.warning(f"[開倉] {symbol} 查倉位失敗：{e}，為安全起見跳過本次")
            return False

        # ── P0-1：本地 state 檢查 ──
        if self.state.has_position(symbol):
            logger.warning(f"[開倉] {symbol} 本地 state 有紀錄，跳過")
            return False

        # ── P2-1：下單冷卻期 ──
        elapsed = self.state.seconds_since_last_order(symbol)
        if elapsed is not None and elapsed < self.order_cooldown_sec:
            logger.info(
                f"[開倉] {symbol} 下單冷卻中（{elapsed:.0f}s / {self.order_cooldown_sec}s），跳過"
            )
            return False

        # 取當前市價
        try:
            ticker      = self.exchange.get_ticker(symbol)
            entry_price = float(ticker['last'])
        except Exception as e:
            logger.warning(f"[開倉] {symbol} 取價格失敗：{e}")
            return False

        # 決定計算基準餘額
        try:
            balance = (
                self.exchange.get_total_balance()
                if self.compound
                else float(self.settings['capital']['total_usdt'])
            )
            self._last_known_balance = balance
        except Exception:
            # B：fallback 到上次成功快取值，不用初始值
            balance = self._last_known_balance
            logger.warning(f"[開倉] {symbol} 取餘額失敗，用快取值 {balance:.2f}")

        # 倉位數量計算（SHIELD 規則：floor 至 3 位小數）
        amount = self.risk_mgr.calculate_position_size(balance, entry_price)
        if amount <= 0:
            logger.warning(f"[開倉] {symbol} 計算數量 <= 0，跳過")
            return False

        # 使用交易所精度修正
        try:
            amount = float(
                self.exchange.exchange.amount_to_precision(symbol, amount)
            )
        except Exception:
            amount = math.floor(amount * 1000) / 1000

        if amount <= 0:
            return False

        # SL / TP
        sl_price = self.strategy.get_stop_loss(entry_price, trade_signal, symbol)
        tp_price = self.strategy.get_take_profit(entry_price, trade_signal, symbol)

        # 下單方向設定
        side       = 'buy'  if trade_signal == 'LONG' else 'sell'
        close_side = 'sell' if trade_signal == 'LONG' else 'buy'
        direction  = '多'   if trade_signal == 'LONG' else '空'

        logger.info(
            f"[開倉] {symbol} 開{direction} | "
            f"價格：{entry_price} | 數量：{amount} | "
            f"SL：{sl_price} | TP：{tp_price}"
        )

        # ── P2-1：先蓋冷卻戳記（即使下單失敗也算，避免無限重試）──
        self.state.mark_order_sent(symbol)

        # ── 送市價主單 ──
        main_order_ok = False
        try:
            self.exchange.create_order(
                symbol     = symbol,
                order_type = 'market',
                side       = side,
                amount     = amount,
            )
            main_order_ok = True
        except Exception as e:
            logger.error(f"[開倉] {symbol} 市價主單 exception：{e}")

        # ── P0-3 + P1-3：reconcile 實際倉位（exception 不代表真的沒成交）──
        time.sleep(1)  # 給交易所 1 秒處理時間
        actual_pos = None
        try:
            actual_pos = self.exchange.get_position(symbol)
        except Exception as e:
            logger.error(f"[開倉] {symbol} reconcile 查倉失敗：{e}")

        actual_contracts = float(actual_pos.get('contracts', 0)) if actual_pos else 0

        if abs(actual_contracts) < 1e-6:  # E：浮點 epsilon
            # 真的沒成交，主單失敗收工
            if not main_order_ok:
                logger.error(f"[開倉] {symbol} 送單失敗且無實際倉位，放棄")
            return False

        # 實際有成交！以交易所實際數量為準
        if abs(actual_contracts - amount) > 1e-6:
            logger.warning(
                f"[開倉] {symbol} 實際成交數量 {actual_contracts} 與計劃 {amount} 不一致，"
                f"以實際為準"
            )
            amount = actual_contracts

        # 以交易所實際開倉價覆寫（若可得）
        actual_entry = float(actual_pos.get('entryPrice') or entry_price)
        entry_price = actual_entry

        # 重新計算 SL/TP（基於實際成交價）
        sl_price = self.strategy.get_stop_loss(entry_price, trade_signal, symbol)
        tp_price = self.strategy.get_take_profit(entry_price, trade_signal, symbol)

        # ── 掛 SL / TP 單（若失敗則緊急平倉，避免裸倉）──
        try:
            self.exchange.create_order(
                symbol     = symbol,
                order_type = 'stop_market',
                side       = close_side,
                amount     = amount,
                params     = {
                    'stopPrice':     sl_price,
                    'reduceOnly':    True,
                    'closePosition': False,
                },
            )
            self.exchange.create_order(
                symbol     = symbol,
                order_type = 'take_profit_market',
                side       = close_side,
                amount     = amount,
                params     = {
                    'stopPrice':     tp_price,
                    'reduceOnly':    True,
                    'closePosition': False,
                },
            )
        except Exception as e:
            logger.error(f"[開倉] {symbol} SL/TP 掛單失敗：{e}，緊急市價平倉避免裸倉")
            try:
                self.exchange.cancel_all_orders(symbol)
                self.exchange.create_order(
                    symbol=symbol, order_type='market',
                    side=close_side, amount=amount,
                    params={'reduceOnly': True},
                )
            except Exception as e2:
                logger.error(f"[開倉] {symbol} 緊急平倉也失敗！請立刻手動處理：{e2}")
            return False

        # 登記至倉位管理器（優先使用固定金額設定）
        position_usdt = self.settings['capital'].get(
            'position_fixed_usdt',
            balance * (self.settings['risk']['position_size_pct'] / 100)
        )
        self.pos_mgr.register_trade(
            symbol        = symbol,
            side          = trade_signal,
            entry_price   = entry_price,
            sl_price      = sl_price,
            tp_price      = tp_price,
            amount        = amount,
            position_usdt = position_usdt,
        )

        # ── P1-1：寫入 state.json ──
        self.state.record_position(
            symbol=symbol, side=trade_signal,
            entry_price=entry_price, amount=amount,
            sl_price=sl_price, tp_price=tp_price,
        )

        # R1/R3：登記持倉狀態（base_ceiling、凍結空單SL）
        # Fix 5：用傳入的 df_1h，不重複 fetch
        base_ceiling = (
            self.strategy.get_triggered_ceiling(df_1h, symbol)
            if trade_signal == 'LONG' and df_1h is not None
            else None
        )
        self.strategy.on_position_opened(symbol, trade_signal, base_ceiling)
        self.state.save_pos_state(symbol, self.strategy.get_pos_state(symbol))
        # Fix 4：anti_repeat 持久化
        self.state.save_anti_repeat(symbol, self.strategy.get_anti_repeat_state(symbol))

        logger.info(
            f"[開倉成功] {symbol} 開{direction} | "
            f"實際進場：{entry_price} | 數量：{amount} | 保證金：${position_usdt:.2f}"
        )
        return True

    # ── 持倉管理（TP1 / TP2 / 加倉）────────────────────────────────
    def _manage_open_positions(self):
        """
        對所有現有持倉執行管理動作：
        - tp1_and_tp2: TP1+TP2 同時觸發 → 直接全平（R4）
        - tp1        : 4H MACD 反向 → 平倉一半 + SL 移至 BE
        - tp2        : TP1 後 1H MACD 反向 → 全平
        - addon      : TP1 前 4H 確認 + 日線 MACD → 加倉（Q2/Q3 更新 SL/TP）
        """
        active_positions = self.pos_mgr.get_active_positions()
        if not active_positions:
            return

        for symbol, pos in active_positions.items():
            signal = pos.get('side', '')
            if not signal:
                continue

            try:
                df_1h = self.exchange.get_ohlcv(symbol, '1h', limit=KLINE_1H)
                df_4h = self.exchange.get_ohlcv(symbol, '4h', limit=KLINE_4H)
                df_1d = self.exchange.get_ohlcv(symbol, '1d', limit=KLINE_1D)
                self.strategy.update_data(df_4h, df_1d)
                self.strategy.update_1h_data(df_1h)
            except Exception as e:
                logger.debug(f"[{symbol}] 持倉管理取 K 線失敗：{e}")
                continue

            action     = self.strategy.get_management_action(symbol, signal)
            entry_price = pos.get('entry_price', 0)
            amount      = pos.get('amount', 0)
            sl_price    = pos.get('sl_price', 0)
            tp_price    = pos.get('tp_price', 0)
            close_side  = 'sell' if signal == 'LONG' else 'buy'

            # ── R4：TP1+TP2 同時觸發 → 直接全平 ─────────────────────
            if action.get('tp1_and_tp2'):
                logger.info(f"[管理] {symbol} TP1+TP2 同時觸發 → 直接全平")
                try:
                    self.exchange.cancel_all_orders(symbol)
                    self.exchange.create_order(
                        symbol=symbol, order_type='market',
                        side=close_side, amount=amount,
                        params={'reduceOnly': True},
                    )
                    self.state.clear_position(symbol)
                    self.strategy.on_position_closed(symbol)
                    logger.info(f"[管理] {symbol} 全平完成")
                except Exception as e:
                    logger.warning(f"[管理] {symbol} 全平失敗：{e}")
                continue

            # ── TP1：平倉一半 + SL 移至 BE ───────────────────────────
            if action.get('tp1'):
                logger.info(f"[管理] {symbol} TP1（4H MACD 反向）→ 平一半，SL 移 BE")
                try:
                    half_amount = math.floor(amount / 2 * 1000) / 1000
                    self.exchange.create_order(
                        symbol=symbol, order_type='market',
                        side=close_side, amount=half_amount,
                        params={'reduceOnly': True},
                    )
                    try:
                        self.exchange.cancel_all_orders(symbol)
                    except Exception:
                        pass
                    self.exchange.create_order(
                        symbol=symbol, order_type='stop_market',
                        side=close_side, amount=half_amount,
                        params={'stopPrice': entry_price, 'reduceOnly': True,
                                'closePosition': False},
                    )
                    logger.info(f"[管理] {symbol} TP1 完成，SL→{entry_price:.2f}")
                    self.state.save_pos_state(
                        symbol, self.strategy.get_pos_state(symbol)
                    )
                except Exception as e:
                    logger.warning(f"[管理] {symbol} TP1 失敗：{e}")

            # ── TP2：全平剩餘 ─────────────────────────────────────────
            if action.get('tp2'):
                logger.info(f"[管理] {symbol} TP2（1H MACD 反向）→ 全平剩餘")
                try:
                    live = self.exchange.get_position(symbol)
                    remain = float(live.get('contracts', 0)) if live else amount / 2
                    if abs(remain) > 1e-6:
                        self.exchange.cancel_all_orders(symbol)
                        self.exchange.create_order(
                            symbol=symbol, order_type='market',
                            side=close_side, amount=remain,
                            params={'reduceOnly': True},
                        )
                    self.state.clear_position(symbol)
                    self.strategy.on_position_closed(symbol)
                    logger.info(f"[管理] {symbol} TP2 全平完成")
                except Exception as e:
                    logger.warning(f"[管理] {symbol} TP2 失敗：{e}")
                continue

            # ── addon：加倉（Q2/Q3：加倉後更新 SL/TP）───────────────
            if action.get('addon'):
                logger.info(f"[管理] {symbol} 加倉條件觸發")
                try:
                    ticker       = self.exchange.get_ticker(symbol)
                    addon_price  = float(ticker['last'])
                    try:
                        b = self.exchange.get_total_balance()
                        self._last_known_balance = b
                    except Exception:
                        b = self._last_known_balance
                    addon_amount = self.risk_mgr.calculate_position_size(b, addon_price)
                    side         = 'buy' if signal == 'LONG' else 'sell'

                    self.exchange.create_order(
                        symbol=symbol, order_type='market',
                        side=side, amount=addon_amount,
                    )
                    new_total = amount + addon_amount
                    try:
                        self.exchange.cancel_all_orders(symbol)
                        if sl_price:
                            self.exchange.create_order(
                                symbol=symbol, order_type='stop_market',
                                side=close_side, amount=new_total,
                                params={'stopPrice': sl_price, 'reduceOnly': True,
                                        'closePosition': False},
                            )
                        if tp_price:
                            self.exchange.create_order(
                                symbol=symbol, order_type='take_profit_market',
                                side=close_side, amount=new_total,
                                params={'stopPrice': tp_price, 'reduceOnly': True,
                                        'closePosition': False},
                            )
                    except Exception as e:
                        logger.warning(f"[管理] {symbol} 加倉後更新 SL/TP 失敗：{e}")

                    self.pos_mgr.update_trade_amount(symbol, new_total, sl_price, tp_price)
                    self.state.update_position_amount(symbol, new_total, sl_price, tp_price)
                    self.state.save_pos_state(symbol, self.strategy.get_pos_state(symbol))
                    logger.info(f"[管理] {symbol} 加倉成功 {addon_amount} @ {addon_price:.2f}，總量 {new_total}")
                except Exception as e:
                    logger.warning(f"[管理] {symbol} 加倉失敗：{e}")

            # ── reduce（僅提示）──────────────────────────────────────
            if action.get('reduce'):
                logger.info(f"[管理] {symbol} 日線 MACD 反向！建議減倉（人工確認）")

    # ── 每日報告 + 重置 ────────────────────────────────────────────────

    def _daily_report_and_reset(self):
        """UTC 00:01 觸發：輸出昨日報告 → 重置日統計"""
        self._print_daily_report()
        self.risk_mgr.reset_daily_stats()
        self.pos_mgr.reset_daily()
        logger.info("[TITAN] 每日統計已重置，新的一天開始")

    def _print_daily_report(self):
        """輸出每日交易報告（繁體中文格式）"""
        try:
            balance = self.exchange.get_total_balance()
        except Exception:
            balance = 0.0

        risk    = self.risk_mgr.get_risk_report()
        summary = self.pos_mgr.get_session_summary()
        trades  = self.pos_mgr.get_closed_trades()

        date_str = datetime.utcnow().strftime('%Y-%m-%d')
        pnl      = risk['daily_pnl_usdt']
        pnl_pct  = risk['daily_pnl_pct']
        sign     = '+' if pnl >= 0 else ''

        print()
        print("=" * 64)
        print(f"  TITAN v1  每日交易報告  ({date_str} UTC)")
        print("=" * 64)
        print(f"  帳戶餘額    : ${balance:>10,.2f} USDT")
        print(f"  初始餘額    : ${self.initial_balance:>10,.2f} USDT")
        print(f"  今日損益    : {sign}{pnl:>+10.2f} USDT  ({sign}{pnl_pct:.2f}%)")
        print(f"  當前回撤    : {risk['current_drawdown_pct']:.2f}%")
        print(f"  今日交易    : {summary['total_trades']} 筆 "
              f"( {summary['wins']} 勝 / {summary['losses']} 敗 )")

        if summary['total_trades'] > 0:
            print(f"  勝率        : {summary['win_rate_pct']:.1f}%")
            total_s = f"{'+'if summary['total_pnl_usdt']>=0 else ''}{summary['total_pnl_usdt']:.2f}"
            print(f"  今日累積損益: {total_s} USDT")
            print(f"  最佳交易    : +${summary['best_trade_usdt']:.2f} USDT")
            print(f"  最差交易    : ${summary['worst_trade_usdt']:.2f} USDT")
            print(f"  連續虧損    : {risk['consecutive_losses']} 次")

            if trades:
                print()
                print(f"  {'進場時間':<15} {'幣種':<8} {'方向':<4} {'出場':<6} {'損益(USDT)':>10}")
                print("  " + "-" * 50)
                for t in trades:
                    side_cn = '多' if t['side'] == 'LONG' else '空'
                    pnl_s   = f"{'+'if t['pnl_usdt']>=0 else ''}{t['pnl_usdt']:.2f}"
                    coin    = t['symbol'].split('/')[0]
                    print(
                        f"  {t['entry_time']:<15} "
                        f"{coin:<8} "
                        f"{side_cn:<4} "
                        f"{t['exit_reason']:<6} "
                        f"{pnl_s:>10}"
                    )

        print("=" * 64)
        print()

    def _print_final_report(self):
        """關閉時輸出最終報告（含 session 完整統計）"""
        try:
            balance = self.exchange.get_total_balance()
        except Exception:
            balance = self.initial_balance

        total_pnl     = balance - self.initial_balance
        total_pnl_pct = (total_pnl / self.initial_balance * 100
                         if self.initial_balance > 0 else 0.0)
        summary       = self.pos_mgr.get_session_summary()
        risk          = self.risk_mgr.get_risk_report()
        sign          = '+' if total_pnl >= 0 else ''

        print()
        print("=" * 64)
        print("  TITAN v1  最終報告（本次 Session）")
        print("=" * 64)
        print(f"  初始餘額    : ${self.initial_balance:>10,.2f} USDT")
        print(f"  最終餘額    : ${balance:>10,.2f} USDT")
        print(f"  總損益      : {sign}{total_pnl:>+10.2f} USDT  ({sign}{total_pnl_pct:.2f}%)")
        print(f"  最大回撤    : {risk['current_drawdown_pct']:.2f}%")
        print(f"  總交易次數  : {summary['total_trades']} 筆 "
              f"( {summary['wins']} 勝 / {summary['losses']} 敗 )")
        if summary['total_trades'] > 0:
            print(f"  勝率        : {summary['win_rate_pct']:.1f}%")
            print(f"  最佳交易    : +${summary['best_trade_usdt']:.2f} USDT")
            print(f"  最差交易    : ${summary['worst_trade_usdt']:.2f} USDT")
        print("=" * 64)
        print()

    def _print_cycle_status(self, balance: float, signals_found: int):
        """每 10 個週期輸出一次狀態摘要"""
        risk    = self.risk_mgr.get_risk_report()
        active  = self.pos_mgr.get_active_count()
        max_pos = self.settings['risk']['max_open_positions']
        pnl     = risk['daily_pnl_usdt']
        sign    = '+' if pnl >= 0 else ''

        logger.info(
            f"[狀態] 餘額：${balance:,.2f} | "
            f"持倉：{active}/{max_pos} | "
            f"今日損益：{sign}{pnl:.2f} USDT | "
            f"今日交易：{risk['trade_count_today']} 筆"
        )

    # ── 初始化輔助 ────────────────────────────────────────────────────

    def _reconcile_on_startup(self):
        """
        啟動對帳（P1-2）：以交易所為最終真相。

        狀況處理：
          - 交易所有倉 + state 有紀錄    → 同步（以交易所數量為準）
          - 交易所有倉 + state 無紀錄    → 標記孤兒倉位，禁開新單
          - 交易所無倉 + state 有紀錄    → 清除過期 state
          - 都無                         → 正常空倉
        """
        logger.info("[TITAN] 啟動對帳：以交易所為真相...")
        try:
            live_positions = self.exchange.get_all_positions()
        except Exception as e:
            logger.error(f"[TITAN] 無法取得交易所持倉，對帳失敗：{e}")
            return

        live_by_symbol = {p['symbol']: p for p in live_positions}
        state_positions = self.state.all_positions()

        self._orphan_symbols: set = set()

        # 1. 交易所有倉：對帳並恢復 pos_mgr（Q5）
        for sym, pos in live_by_symbol.items():
            contracts = float(pos.get('contracts', 0))
            exchange_side = pos.get('side', '').upper()
            entry = float(pos.get('entryPrice') or 0)

            if sym in state_positions:
                sp = state_positions[sym]
                # 以交易所數量為準
                sl  = sp.get('sl_price', 0)
                tp  = sp.get('tp_price', 0)
                sid = sp.get('side', exchange_side)
                pos_usdt = sp.get('position_usdt',
                    self.settings['capital'].get('position_fixed_usdt', 100))
                self.pos_mgr.restore_trade(
                    symbol=sym, side=sid,
                    entry_price=sp.get('entry_price', entry),
                    sl_price=sl, tp_price=tp,
                    amount=contracts, position_usdt=pos_usdt,
                )
                # 恢復策略 pos_state（Q4）
                saved_ps = self.state.get_pos_state(sym)
                if saved_ps:
                    self.strategy.restore_pos_state(sym, saved_ps)
                # Fix 4：恢復 anti_repeat
                saved_ar = self.state.get_anti_repeat(sym)
                if saved_ar:
                    self.strategy.restore_anti_repeat(sym, saved_ar)
                logger.info(
                    f"[對帳] {sym} 恢復持倉 {sid} {contracts} @ {entry} | "
                    f"tp1_done={saved_ps.get('tp1_done') if saved_ps else '?'}"
                )
            else:
                logger.warning(
                    f"[對帳] {sym} 交易所有倉但本地無紀錄 → 孤兒倉位 "
                    f"{exchange_side} {contracts} @ {entry}，禁止開新單"
                )
                self._orphan_symbols.add(sym)

        # 2. state 有紀錄但交易所無倉：清除過期
        for sym in list(state_positions.keys()):
            if sym not in live_by_symbol:
                logger.info(f"[對帳] {sym} state 有紀錄但交易所無倉，清除過期 state")
                self.state.clear_position(sym)

        # 恢復 anti_repeat（無論有無持倉，防重複追單狀態跨重啟保留）
        for sym, ar in self.state.all_anti_repeat().items():
            self.strategy.restore_anti_repeat(sym, ar)

        if not live_by_symbol and not state_positions:
            logger.info("[對帳] 無任何持倉，正常啟動")

    def _init_boxes(self):
        """啟動時為每個 symbol 初始化箱體（手動 or 自動偵測）"""
        try:
            symbols = self.scanner.get_tradeable_symbols()
        except Exception:
            return
        for symbol in symbols:
            try:
                df_1d = self.exchange.get_ohlcv(symbol, '1d', limit=KLINE_1D)
                self.strategy.init_box(symbol, df_1d)
            except Exception as e:
                logger.warning(f"[{symbol}] 箱體初始化失敗：{e}")
                self.strategy.init_box(symbol)

    def _setup_symbols(self):
        """預先設定所有可交易幣種的槓桿與保證金模式"""
        leverage    = self.settings['risk']['leverage']
        margin_type = self.settings['risk']['margin_type']

        try:
            symbols = self.scanner.get_tradeable_symbols()
        except Exception as e:
            logger.warning(f"[TITAN] 取幣種列表失敗，跳過預設定：{e}")
            return

        margin_label = '全倉' if margin_type == 'cross' else '逐倉'
        logger.info(
            f"[TITAN] 設定 {len(symbols)} 個幣種 "
            f"（{leverage}x 槓桿 / {margin_label}）..."
        )
        for symbol in symbols:
            try:
                self.exchange.set_leverage(symbol, leverage)
                self.exchange.set_margin_type(symbol, margin_type)
            except Exception as e:
                logger.debug(f"[{symbol}] 槓桿/保證金設定：{e}")

    def _handle_signal(self, signum, frame):
        """Unix 訊號處理（Ctrl+C / kill）"""
        logger.info(f"[TITAN] 收到系統訊號 {signum}，啟動優雅關閉...")
        self.stop()
        sys.exit(0)


# ══════════════════════════════════════════════════════════════════════
# 進入點
# ══════════════════════════════════════════════════════════════════════

def main():
    print()
    print("=" * 60)
    print("  TITAN v1")
    print("  Trading Intelligent Tactical Automation Network")
    print("=" * 60)
    print()

    # 載入設定
    try:
        settings = load_settings()
    except (FileNotFoundError, ValueError) as e:
        print(f"[錯誤] 設定檔載入失敗：{e}")
        sys.exit(1)

    mode = settings.get('mode', 'testnet')

    # 正式網安全確認
    if mode == 'live':
        print()
        print("  !! 警告：即將連接正式網（真實資金！）!!")
        print("  !! 確認繼續請按 Enter，取消請按 Ctrl+C   !!")
        print()
        try:
            input("  >>> 按 Enter 確認：")
        except KeyboardInterrupt:
            print("\n  已取消，安全退出。")
            sys.exit(0)

    # 啟動機器人
    try:
        bot = TitanBot(settings)
        bot.start()
    except ConnectionError as e:
        logger.error(f"連線失敗：{e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"啟動錯誤：{e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()

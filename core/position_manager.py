"""
TITAN v1 — 倉位管理模組（Phase 3 / REX + SHIELD 聯合實作）

職責：
  - 追蹤 TITAN 開出的所有倉位（active_trades）
  - 每次掃描週期與交易所同步，偵測 SL/TP 觸發後自動平倉
  - 計算每筆交易實現損益（含手續費）
  - 提供 session 統計摘要供每日報告使用

設計原則：
  - 狀態完全 in-memory，重啟重置（可接受）
  - is_in_position() 以 active_trades 為準，不每次查交易所（節省 API 配額）
  - sync_positions() 是唯一與交易所同步的入口
  - 平倉原因推斷：比對當前市價與 SL/TP 距離，取近者
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from core.exchange import Exchange
from utils.logger import get_logger

logger = get_logger()

TAKER_FEE = 0.0005  # 0.05% 單邊手續費


# ══════════════════════════════════════════════════════════════════════
# TradeRecord：單筆交易的完整生命週期記錄
# ══════════════════════════════════════════════════════════════════════

@dataclass
class TradeRecord:
    """
    記錄一筆交易從開倉到平倉的所有資訊。
    開倉時建立，平倉後呼叫 close() 填入出場資訊。
    """

    # 開倉時填入
    symbol:        str
    side:          str          # 'LONG' or 'SHORT'
    entry_price:   float
    sl_price:      float
    tp_price:      float
    amount:        float        # 合約數量（幣數）
    position_usdt: float        # 保證金金額（不含槓桿）
    entry_time:    datetime

    # 平倉後填入
    exit_price:   float = 0.0
    exit_time:    Optional[datetime] = None
    exit_reason:  str = ''      # 'SL' | 'TP' | 'MANUAL'
    pnl_usdt:     float = 0.0
    pnl_pct:      float = 0.0
    is_closed:    bool = False

    def close(
        self,
        exit_price:  float,
        exit_time:   datetime,
        exit_reason: str,
        leverage:    int,
    ) -> None:
        """
        填入出場資訊並計算損益。

        公式（與回測引擎保持一致）：
            raw_pct = (exit - entry) / entry  （多）
                    = (entry - exit) / entry  （空）
            net_pct = raw_pct × leverage - fee_in - fee_out
            pnl_usdt = position_usdt × net_pct
        """
        self.exit_price  = exit_price
        self.exit_time   = exit_time
        self.exit_reason = exit_reason
        self.is_closed   = True

        if self.side == 'LONG':
            raw_pct = (exit_price - self.entry_price) / self.entry_price
        else:
            raw_pct = (self.entry_price - exit_price) / self.entry_price

        net_pct      = raw_pct * leverage - TAKER_FEE * 2
        self.pnl_pct  = net_pct * 100
        self.pnl_usdt = self.position_usdt * net_pct

    def to_dict(self) -> dict:
        return {
            'symbol':      self.symbol,
            'side':        self.side,
            'entry_time':  str(self.entry_time)[:16],
            'exit_time':   str(self.exit_time)[:16] if self.exit_time else '',
            'entry_price': self.entry_price,
            'exit_price':  self.exit_price,
            'sl_price':    self.sl_price,
            'tp_price':    self.tp_price,
            'exit_reason': self.exit_reason,
            'pnl_usdt':    round(self.pnl_usdt, 2),
            'pnl_pct':     round(self.pnl_pct, 2),
        }


# ══════════════════════════════════════════════════════════════════════
# PositionManager：倉位生命週期管理
# ══════════════════════════════════════════════════════════════════════

class PositionManager:
    """
    倉位管理器。

    active_trades  : dict[symbol, TradeRecord]  ── 當前持倉
    closed_trades  : list[TradeRecord]           ── 已平倉（session 內）

    使用流程：
        1. 開倉後呼叫 register_trade() 登記
        2. 每個掃描週期呼叫 sync_positions() 偵測平倉事件
        3. 收到平倉列表後，呼叫 RiskManager.record_trade() 更新風控狀態
        4. 每日 00:01 呼叫 reset_daily() 清空 closed_trades
    """

    def __init__(self, exchange: Exchange, settings: dict):
        self.exchange              = exchange
        self.leverage: int         = settings['risk']['leverage']
        self.active_trades: dict[str, TradeRecord] = {}
        self.closed_trades: list[TradeRecord]       = []
        # 防誤判平倉：連續 missing 次數計數（2 次才算真平倉）
        self._missing_count: dict[str, int] = {}
        # 剛開倉 60 秒保護期（避免 Demo API 剛成交還沒 index 到就被誤判）
        self.GRACE_PERIOD_SEC: int = 60

    # ── 開倉登記 ──────────────────────────────────────────────────────

    def register_trade(
        self,
        symbol:        str,
        side:          str,
        entry_price:   float,
        sl_price:      float,
        tp_price:      float,
        amount:        float,
        position_usdt: float,
    ) -> TradeRecord:
        """開倉後登記倉位，加入 active_trades。"""
        trade = TradeRecord(
            symbol        = symbol,
            side          = side,
            entry_price   = entry_price,
            sl_price      = sl_price,
            tp_price      = tp_price,
            amount        = amount,
            position_usdt = position_usdt,
            entry_time    = datetime.now(),
        )
        self.active_trades[symbol] = trade
        logger.info(
            f"[倉位] 登記 {symbol} {side} | "
            f"進場 {entry_price} | SL {sl_price} | TP {tp_price} | "
            f"合約 {amount} | 保證金 ${position_usdt:.2f}"
        )
        return trade

    def restore_trade(
        self,
        symbol:        str,
        side:          str,
        entry_price:   float,
        sl_price:      float,
        tp_price:      float,
        amount:        float,
        position_usdt: float,
    ) -> TradeRecord:
        """Q5：啟動對帳時，從 state.json 恢復 active_trade（不重複 log）"""
        trade = TradeRecord(
            symbol        = symbol,
            side          = side,
            entry_price   = entry_price,
            sl_price      = sl_price,
            tp_price      = tp_price,
            amount        = amount,
            position_usdt = position_usdt,
            entry_time    = datetime.now(),
        )
        self.active_trades[symbol] = trade
        return trade

    def update_trade_amount(self, symbol: str, new_amount: float,
                            new_sl: float, new_tp: float):
        """Q2+Q3：加倉後更新 active_trade 的數量與 SL/TP"""
        trade = self.active_trades.get(symbol)
        if trade:
            trade.amount   = new_amount
            trade.sl_price = new_sl
            trade.tp_price = new_tp

    # ── 同步偵測（核心）──────────────────────────────────────────────

    def sync_positions(self) -> list[TradeRecord]:
        """
        與交易所同步，偵測已被 SL/TP 觸發而平倉的倉位。

        三層保險：
          ① 剛開倉 60 秒保護期 → 絕不判定平倉
          ② 消失時先單獨查一次（雙重確認）
          ③ 連續 2 次確認消失 → 才當真平倉
        """
        if not self.active_trades:
            return []

        try:
            live_positions = self.exchange.get_all_positions()
            live_symbols = {p['symbol'] for p in live_positions}
        except Exception as e:
            logger.warning(f"[倉位] sync_positions 失敗，跳過本次偵測：{e}")
            return []

        newly_closed: list[TradeRecord] = []
        now = datetime.now()

        for symbol, trade in list(self.active_trades.items()):
            if symbol in live_symbols:
                # 倉位存在 → 重置計數
                self._missing_count.pop(symbol, None)
                continue

            # ── 保險 ①：剛開倉 60 秒保護期 ──
            elapsed = (now - trade.entry_time).total_seconds()
            if elapsed < self.GRACE_PERIOD_SEC:
                logger.warning(
                    f"[倉位] {symbol} 從列表消失但仍在保護期內 "
                    f"({elapsed:.0f}s/{self.GRACE_PERIOD_SEC}s)，忽略"
                )
                continue

            # ── 保險 ②：雙重確認（單獨查一次）──
            try:
                pos_direct = self.exchange.get_position(symbol)
                if pos_direct and float(pos_direct.get('contracts', 0)) != 0:
                    logger.warning(
                        f"[倉位] {symbol} bulk 查不到但直查存在 → API 不穩，忽略"
                    )
                    self._missing_count.pop(symbol, None)
                    continue
            except Exception as e:
                logger.warning(f"[倉位] {symbol} 直查失敗，保守忽略本次：{e}")
                continue

            # ── 保險 ③：連續 2 次消失才算真平倉 ──
            self._missing_count[symbol] = self._missing_count.get(symbol, 0) + 1
            if self._missing_count[symbol] < 2:
                logger.warning(
                    f"[倉位] {symbol} 第 {self._missing_count[symbol]} 次消失，"
                    f"等下輪確認後再判定"
                )
                continue

            # 三層都通過 → 真平倉
            exit_price, exit_reason = self._infer_exit(trade)
            trade.close(
                exit_price  = exit_price,
                exit_time   = now,
                exit_reason = exit_reason,
                leverage    = self.leverage,
            )
            self.closed_trades.append(trade)
            del self.active_trades[symbol]
            self._missing_count.pop(symbol, None)
            newly_closed.append(trade)

            sign = '+' if trade.pnl_usdt >= 0 else ''
            logger.info(
                f"[倉位] 偵測平倉 {symbol} {trade.side} | "
                f"原因：{exit_reason} | "
                f"推算出場：{exit_price} | "
                f"損益：{sign}{trade.pnl_usdt:.2f} USDT ({sign}{trade.pnl_pct:.2f}%)"
            )

        return newly_closed

    # ── 緊急平倉（Ctrl+C / 熔斷）────────────────────────────────────

    def emergency_close_all(self) -> list[TradeRecord]:
        """
        強制平倉所有持倉。
        Ctrl+C 或帳戶回撤觸發時呼叫。
        """
        closed: list[TradeRecord] = []

        for symbol, trade in list(self.active_trades.items()):
            logger.info(f"[倉位] 緊急平倉：{symbol}...")

            # Step 1：取消所有掛單（SL/TP 止損止盈單）
            try:
                self.exchange.cancel_all_orders(symbol)
            except Exception as e:
                logger.warning(f"[倉位] 取消掛單失敗 {symbol}：{e}")

            # Step 2：查詢當前倉位後市價平倉
            exit_price = trade.entry_price  # fallback
            try:
                pos = self.exchange.get_position(symbol)
                if pos and float(pos.get('contracts', 0)) != 0:
                    close_side = 'sell' if trade.side == 'LONG' else 'buy'
                    self.exchange.create_order(
                        symbol     = symbol,
                        order_type = 'market',
                        side       = close_side,
                        amount     = float(pos['contracts']),
                        params     = {'reduceOnly': True},
                    )
            except Exception as e:
                logger.error(f"[倉位] 緊急平倉送單失敗 {symbol}：{e}")

            # Step 3：取得出場價
            try:
                ticker     = self.exchange.get_ticker(symbol)
                exit_price = float(ticker.get('last', trade.entry_price))
            except Exception:
                pass

            trade.close(
                exit_price  = exit_price,
                exit_time   = datetime.now(),
                exit_reason = 'MANUAL',
                leverage    = self.leverage,
            )
            self.closed_trades.append(trade)
            closed.append(trade)
            logger.info(f"[倉位] 緊急平倉完成：{symbol}")

        self.active_trades.clear()
        return closed

    # ── 查詢介面 ──────────────────────────────────────────────────────

    def is_in_position(self, symbol: str) -> bool:
        """是否持有指定 symbol 的倉位"""
        return symbol in self.active_trades

    def get_active_count(self) -> int:
        """當前持倉數量"""
        return len(self.active_trades)

    def get_active_symbols(self) -> list:
        return list(self.active_trades.keys())

    def get_active_positions(self) -> dict:
        """
        回傳所有當前持倉的關鍵資訊。
        格式：{ symbol: {'side': 'LONG'/'SHORT', 'entry_price': float, 'amount': float} }
        """
        return {
            symbol: {
                'side':        trade.side,
                'entry_price': trade.entry_price,
                'amount':      trade.amount,
            }
            for symbol, trade in self.active_trades.items()
        }

    def get_session_summary(self) -> dict:
        """本次 session 所有已平倉交易的統計摘要"""
        trades = self.closed_trades
        if not trades:
            return {
                'total_trades':     0,
                'wins':             0,
                'losses':           0,
                'win_rate_pct':     0.0,
                'total_pnl_usdt':   0.0,
                'best_trade_usdt':  0.0,
                'worst_trade_usdt': 0.0,
            }

        wins   = [t for t in trades if t.pnl_usdt > 0]
        losses = [t for t in trades if t.pnl_usdt <= 0]
        pnls   = [t.pnl_usdt for t in trades]

        return {
            'total_trades':     len(trades),
            'wins':             len(wins),
            'losses':           len(losses),
            'win_rate_pct':     round(len(wins) / len(trades) * 100, 1),
            'total_pnl_usdt':   round(sum(pnls), 2),
            'best_trade_usdt':  round(max(pnls), 2),
            'worst_trade_usdt': round(min(pnls), 2),
        }

    def get_closed_trades(self) -> list:
        return [t.to_dict() for t in self.closed_trades]

    def reset_daily(self) -> None:
        """每日重置：清空 closed_trades（報告已輸出後呼叫）"""
        logger.info(f"[倉位] 每日重置，清空 {len(self.closed_trades)} 筆已平倉記錄")
        self.closed_trades.clear()

    # ── 內部工具 ──────────────────────────────────────────────────────

    def _infer_exit(self, trade: TradeRecord) -> tuple:
        """
        C：推斷平倉出場價與原因。

        優先查詢交易所歷史成交：找最近已成交的 STOP_MARKET 或 TAKE_PROFIT_MARKET。
        失敗時 fallback 到距離啟發式（保守假設 SL）。
        """
        try:
            # 嘗試查最近 10 筆已成交訂單
            orders = self.exchange.exchange.fetch_orders(
                trade.symbol, limit=10,
                params={"ordStatus": "filled"}
            )
            # 找最近 reduceOnly 已成交的出場單
            close_side = 'sell' if trade.side == 'LONG' else 'buy'
            for o in reversed(orders):
                if (o.get('status') == 'closed'
                        and o.get('side') == close_side
                        and o.get('reduceOnly')):
                    fill_price  = float(o.get('average') or o.get('price') or 0)
                    order_type  = str(o.get('type', '')).lower()
                    if fill_price > 0:
                        reason = 'TP' if 'take_profit' in order_type else 'SL'
                        return fill_price, reason
        except Exception as e:
            logger.debug(f"[倉位] _infer_exit 查歷史訂單失敗 {trade.symbol}：{e}")

        # Fallback：距離啟發式
        try:
            ticker     = self.exchange.get_ticker(trade.symbol)
            last_price = float(ticker.get('last', 0))
            if last_price > 0:
                sl_dist = abs(last_price - trade.sl_price)
                tp_dist = abs(last_price - trade.tp_price)
                if sl_dist <= tp_dist:
                    return trade.sl_price, 'SL'
                else:
                    return trade.tp_price, 'TP'
        except Exception as e:
            logger.debug(f"[倉位] _infer_exit fallback 失敗 {trade.symbol}：{e}")

        # fallback：保守假設為止損
        return trade.sl_price, 'SL'

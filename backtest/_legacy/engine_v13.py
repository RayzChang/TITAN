"""
TITAN v1.3 — 多時框回測引擎

特性：
  - 完整支援 RangeBreakout v1.2 策略（箱體狀態 + 多時框）
  - 1H 進場、4H TP1、1H TP2、4H+日線 加倉
  - SL/TP 用當根 K 線 high/low 判定觸發
  - 嚴禁 lookahead bias：訊號只用已收線（iloc[-2]）
  - OOS 友善：可指定起訖日期
"""

import math
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

TAKER_FEE   = 0.0005   # 0.05% 單邊
WARMUP_BARS = 50       # 1H 暖機根數


class Trade:
    __slots__ = (
        'entry_time', 'exit_time', 'side',
        'entry_price', 'exit_price', 'amount',
        'pnl_pct', 'pnl_usdt', 'exit_reason',
        'tp1_done', 'addon_done',
        'sl_price', 'tp_price',
    )

    def __init__(self, entry_time, side, entry_price, amount, sl, tp):
        self.entry_time = entry_time
        self.exit_time  = None
        self.side       = side
        self.entry_price = entry_price
        self.exit_price = None
        self.amount     = amount
        self.sl_price   = sl
        self.tp_price   = tp
        self.pnl_pct    = 0.0
        self.pnl_usdt   = 0.0
        self.exit_reason = ''
        self.tp1_done   = False
        self.addon_done = False

    def close(self, exit_time, exit_price, exit_reason,
              position_usdt, leverage):
        self.exit_time   = exit_time
        self.exit_price  = exit_price
        self.exit_reason = exit_reason
        if self.side == 'LONG':
            raw = (exit_price - self.entry_price) / self.entry_price
        else:
            raw = (self.entry_price - exit_price) / self.entry_price
        net = raw * leverage - TAKER_FEE * 2
        self.pnl_pct  = net * 100
        self.pnl_usdt = position_usdt * net

    def to_dict(self):
        return {
            'entry_time':  self.entry_time,
            'exit_time':   self.exit_time,
            'side':        self.side,
            'entry_price': self.entry_price,
            'exit_price':  self.exit_price,
            'pnl_pct':     round(self.pnl_pct, 4),
            'pnl_usdt':    round(self.pnl_usdt, 2),
            'exit_reason': self.exit_reason,
        }


class V13BacktestEngine:
    """v1.3 策略回測引擎（含完整箱體 + 多時框邏輯）"""

    def __init__(self, strategy, settings: dict, symbol: str):
        self.strategy = strategy
        self.symbol   = symbol
        self.settings = settings

        capital = settings.get('capital', {})
        risk    = settings.get('risk',    {})
        self.initial_capital   = float(capital.get('total_usdt', 5000))
        self.position_usdt     = float(capital.get('position_fixed_usdt', 100))
        self.leverage          = int(risk.get('leverage', 100))

    def run(self, df_1h: pd.DataFrame, df_4h: pd.DataFrame,
            df_1d: pd.DataFrame, init_box_with_manual: bool = True) -> dict:
        """
        執行回測。

        df_1h : 1H K 線 (DatetimeIndex, UTC)
        df_4h : 4H K 線
        df_1d : 1D K 線
        """
        df_1h = df_1h.sort_index()
        df_4h = df_4h.sort_index()
        df_1d = df_1d.sort_index()

        if len(df_1h) < WARMUP_BARS + 10:
            raise ValueError(f'1H K 線不足，需要至少 {WARMUP_BARS + 10} 根')

        # 初始化箱體：用第 50 根 1H 對應之前的日線資料
        warmup_time = df_1h.index[WARMUP_BARS]
        init_daily  = df_1d[df_1d.index < warmup_time]
        if init_box_with_manual:
            self.strategy.init_box(self.symbol, init_daily)
        else:
            # 強制走自動偵測（不讀 manual_boxes）
            self.strategy._manual_boxes = {}
            self.strategy.init_box(self.symbol, init_daily)

        capital  = self.initial_capital
        trades   = []
        current  = None              # 當前持倉 Trade
        last_daily_processed_ts = None  # 已處理到的日線時間
        rolling_balance_curve = [(df_1h.index[WARMUP_BARS], capital)]

        for i in range(WARMUP_BARS, len(df_1h)):
            bar      = df_1h.iloc[i]
            bar_time = df_1h.index[i]

            # 注入截至本根（不含本根）的多時框資料
            sub_4h = df_4h[df_4h.index < bar_time]
            sub_1d = df_1d[df_1d.index < bar_time]
            sub_1h = df_1h.iloc[:i + 1]   # 含本根（將被當成 forming）
            self.strategy.update_data(sub_4h, sub_1d)
            self.strategy.update_1h_data(sub_1h)

            # ── 1. 日線收盤後更新箱體（每天最多 1 次）──
            if len(sub_1d) > 0:
                last_d_ts = sub_1d.index[-1]
                if last_daily_processed_ts is None or last_d_ts > last_daily_processed_ts:
                    last_d_ts = sub_1d.index[-1]
                    last_d    = sub_1d.iloc[-1]
                    self.strategy.update_box(
                        symbol         = self.symbol,
                        current_high   = float(last_d['high']),
                        current_close  = float(last_d['close']),
                        current_volume = float(last_d['volume']),
                    )
                    last_daily_processed_ts = last_d_ts

            # ── 2. 若有持倉：先檢查 SL / TP ──
            if current is not None:
                hit_sl = (current.side == 'LONG' and bar['low']  <= current.sl_price) \
                      or (current.side == 'SHORT' and bar['high'] >= current.sl_price)
                hit_tp = (current.side == 'LONG' and bar['high'] >= current.tp_price) \
                      or (current.side == 'SHORT' and bar['low']  <= current.tp_price)

                if hit_sl or hit_tp:
                    # 同時觸發保守取 SL
                    exit_price  = current.sl_price if hit_sl else current.tp_price
                    exit_reason = 'SL' if hit_sl else 'TP'
                    current.close(
                        exit_time     = bar_time,
                        exit_price    = exit_price,
                        exit_reason   = exit_reason,
                        position_usdt = self.position_usdt,
                        leverage      = self.leverage,
                    )
                    capital += current.pnl_usdt
                    trades.append(current)
                    self.strategy.on_position_closed(self.symbol)
                    self.strategy.rebuild_box_if_invalidated(self.symbol)
                    rolling_balance_curve.append((bar_time, capital))
                    current = None
                    continue

                # ── 2b. 持倉中：檢查 TP1 / TP2 / addon ──
                action = self.strategy.get_management_action(self.symbol, current.side)

                if action.get('tp1_and_tp2'):
                    # 全平
                    current.close(
                        exit_time     = bar_time,
                        exit_price    = float(bar['close']),
                        exit_reason   = 'TP1+TP2',
                        position_usdt = self.position_usdt,
                        leverage      = self.leverage,
                    )
                    capital += current.pnl_usdt
                    trades.append(current)
                    self.strategy.on_position_closed(self.symbol)
                    rolling_balance_curve.append((bar_time, capital))
                    current = None
                    continue

                if action.get('tp1') and not current.tp1_done:
                    # 平一半 + SL 移到 BE
                    half_pnl_pct = self._calc_pnl_pct(
                        current.side, current.entry_price, float(bar['close'])
                    )
                    half_position = self.position_usdt / 2
                    half_net = half_pnl_pct * self.leverage / 100 - TAKER_FEE * 2
                    capital += half_position * half_net
                    current.tp1_done = True
                    current.sl_price = current.entry_price  # 保本
                    rolling_balance_curve.append((bar_time, capital))

                if action.get('tp2') and current.tp1_done:
                    # 平剩餘
                    remaining = self.position_usdt / 2
                    raw = self._calc_pnl_pct(
                        current.side, current.entry_price, float(bar['close'])
                    )
                    net = raw * self.leverage / 100 - TAKER_FEE * 2
                    half_pnl = remaining * net
                    current.exit_time   = bar_time
                    current.exit_price  = float(bar['close'])
                    current.exit_reason = 'TP2'
                    current.pnl_usdt    = current.pnl_usdt + half_pnl  # accumulate（half 已計入 capital）
                    capital += half_pnl
                    trades.append(current)
                    self.strategy.on_position_closed(self.symbol)
                    rolling_balance_curve.append((bar_time, capital))
                    current = None
                    continue

            # ── 3. 無持倉：計算進場訊號 ──
            if current is None:
                signal = self.strategy.calculate_signals(sub_1h, self.symbol)
                if signal in ('LONG', 'SHORT') and i + 1 < len(df_1h):
                    next_bar = df_1h.iloc[i + 1]
                    entry_price = float(next_bar['open'])
                    entry_time  = df_1h.index[i + 1]

                    # 取 base_ceiling（給策略登記）
                    base_ceil = (
                        self.strategy.get_triggered_ceiling(sub_1h, self.symbol)
                        if signal == 'LONG' else None
                    )

                    sl = self.strategy.get_stop_loss(entry_price, signal, self.symbol)
                    tp = self.strategy.get_take_profit(entry_price, signal, self.symbol)
                    amt = self.position_usdt * self.leverage / entry_price

                    current = Trade(entry_time, signal, entry_price, amt, sl, tp)
                    self.strategy.on_position_opened(self.symbol, signal, base_ceil)

        # 結束強制平倉
        if current is not None:
            last_bar = df_1h.iloc[-1]
            current.close(
                exit_time     = df_1h.index[-1],
                exit_price    = float(last_bar['close']),
                exit_reason   = 'EOD',
                position_usdt = self.position_usdt,
                leverage      = self.leverage,
            )
            capital += current.pnl_usdt
            trades.append(current)

        return self._calc_metrics(trades, capital, rolling_balance_curve, df_1h)

    @staticmethod
    def _calc_pnl_pct(side, entry, exit):
        if side == 'LONG':
            return (exit - entry) / entry * 100
        else:
            return (entry - exit) / entry * 100

    def _calc_metrics(self, trades, final_cap, curve, df) -> dict:
        if not trades:
            return {
                'start_time': df.index[WARMUP_BARS],
                'end_time':   df.index[-1],
                'total_trades': 0,
                'final_capital': self.initial_capital,
                'total_return_pct': 0.0,
                'win_rate_pct': 0.0,
                'avg_win_usdt': 0.0,
                'avg_loss_usdt': 0.0,
                'max_drawdown_pct': 0.0,
                'profit_factor': 0.0,
                'sharpe_ratio': 0.0,
                'trades': [],
            }

        wins   = [t for t in trades if t.pnl_usdt > 0]
        losses = [t for t in trades if t.pnl_usdt <= 0]

        win_rate = len(wins) / len(trades) * 100
        avg_win  = float(np.mean([t.pnl_usdt for t in wins]))   if wins   else 0.0
        avg_loss = float(np.mean([t.pnl_usdt for t in losses])) if losses else 0.0
        total_win  = sum(t.pnl_usdt for t in wins)
        total_loss = abs(sum(t.pnl_usdt for t in losses))
        pf = total_win / total_loss if total_loss > 0 else float('inf')

        # 回撤計算（用 rolling balance curve）
        peak = self.initial_capital
        max_dd = 0.0
        for _, bal in curve:
            if bal > peak:
                peak = bal
            dd = (peak - bal) / peak * 100
            if dd > max_dd:
                max_dd = dd

        # Sharpe（簡化版）
        returns = np.array([t.pnl_usdt for t in trades])
        sr = 0.0
        if len(returns) > 1 and returns.std(ddof=1) > 0:
            sr = float(returns.mean() / returns.std(ddof=1) * math.sqrt(len(returns)))

        return {
            'start_time': trades[0].entry_time,
            'end_time':   trades[-1].exit_time,
            'total_trades': len(trades),
            'winning_trades': len(wins),
            'losing_trades':  len(losses),
            'final_capital': round(final_cap, 2),
            'total_return_pct': round((final_cap - self.initial_capital) / self.initial_capital * 100, 2),
            'win_rate_pct': round(win_rate, 2),
            'avg_win_usdt':  round(avg_win, 2),
            'avg_loss_usdt': round(avg_loss, 2),
            'max_drawdown_pct': round(max_dd, 2),
            'profit_factor': round(pf, 2) if pf != float('inf') else 'inf',
            'sharpe_ratio': round(sr, 2),
            'trades': [t.to_dict() for t in trades],
        }

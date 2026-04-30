"""
TITAN — 簡易回測引擎（給 candidate 策略用）

特性：
  - 接受任何有 calculate_signals(df_1h, symbol) / get_stop_loss / get_take_profit 介面的策略
  - 可選注入 4H 資料（給趨勢過濾用）
  - 單筆 SL/TP，不處理 TP1/TP2/加倉
  - 同樣禁止 lookahead bias
"""

import math
from typing import Optional

import numpy as np
import pandas as pd


TAKER_FEE = 0.0005
WARMUP_BARS = 60


class SimpleTrade:
    __slots__ = ('entry_time', 'exit_time', 'side',
                 'entry_price', 'exit_price', 'sl_price', 'tp_price',
                 'pnl_pct', 'pnl_usdt', 'exit_reason')

    def __init__(self, t, side, entry, sl, tp):
        self.entry_time = t
        self.exit_time  = None
        self.side       = side
        self.entry_price = entry
        self.exit_price = None
        self.sl_price   = sl
        self.tp_price   = tp
        self.pnl_pct    = 0.0
        self.pnl_usdt   = 0.0
        self.exit_reason = ''

    def close(self, t, price, reason, position_usdt, leverage):
        self.exit_time   = t
        self.exit_price  = price
        self.exit_reason = reason
        if self.side == 'LONG':
            raw = (price - self.entry_price) / self.entry_price
        else:
            raw = (self.entry_price - price) / self.entry_price
        net = raw * leverage - TAKER_FEE * 2
        self.pnl_pct  = net * 100
        self.pnl_usdt = position_usdt * net

    def to_dict(self):
        return {
            'entry_time':  self.entry_time, 'exit_time': self.exit_time,
            'side': self.side,
            'entry_price': self.entry_price, 'exit_price': self.exit_price,
            'pnl_pct': round(self.pnl_pct, 4),
            'pnl_usdt': round(self.pnl_usdt, 2),
            'exit_reason': self.exit_reason,
        }


class SimpleBacktestEngine:
    def __init__(self, strategy, settings: dict, symbol: str = ''):
        self.strategy = strategy
        self.symbol   = symbol
        self.settings = settings

        cap = settings.get('capital', {})
        risk = settings.get('risk', {})
        self.initial_capital = float(cap.get('total_usdt', 5000))
        self.position_usdt   = float(cap.get('position_fixed_usdt', 100))
        self.leverage        = int(risk.get('leverage', 100))

    def run(self, df_1h: pd.DataFrame,
            df_4h: Optional[pd.DataFrame] = None,
            df_1d: Optional[pd.DataFrame] = None) -> dict:
        df_1h = df_1h.sort_index()
        if df_4h is not None:
            df_4h = df_4h.sort_index()

        if len(df_1h) < WARMUP_BARS + 5:
            raise ValueError(f'1H 資料不足，需 {WARMUP_BARS + 5} 根')

        capital = self.initial_capital
        trades = []
        current: Optional[SimpleTrade] = None
        equity_curve = [(df_1h.index[WARMUP_BARS], capital)]

        for i in range(WARMUP_BARS, len(df_1h)):
            bar = df_1h.iloc[i]
            t   = df_1h.index[i]

            # 注入 4H（截至本根之前已收）
            if df_4h is not None and hasattr(self.strategy, 'update_data'):
                sub_4h = df_4h[df_4h.index < t]
                self.strategy.update_data(sub_4h, df_1d)

            # SL/TP 檢查
            if current is not None:
                if current.side == 'LONG':
                    hit_sl = bar['low']  <= current.sl_price
                    hit_tp = bar['high'] >= current.tp_price
                else:
                    hit_sl = bar['high'] >= current.sl_price
                    hit_tp = bar['low']  <= current.tp_price

                if hit_sl or hit_tp:
                    price  = current.sl_price if hit_sl else current.tp_price
                    reason = 'SL' if hit_sl else 'TP'
                    current.close(t, price, reason, self.position_usdt, self.leverage)
                    capital += current.pnl_usdt
                    trades.append(current)
                    equity_curve.append((t, capital))
                    current = None
                    continue

            # 進場訊號
            if current is None:
                sub_1h = df_1h.iloc[:i + 1]
                try:
                    sig = self.strategy.calculate_signals(sub_1h, self.symbol)
                except TypeError:
                    sig = self.strategy.calculate_signals(sub_1h)

                if sig in ('LONG', 'SHORT') and i + 1 < len(df_1h):
                    next_bar = df_1h.iloc[i + 1]
                    entry = float(next_bar['open'])
                    et    = df_1h.index[i + 1]
                    try:
                        sl = self.strategy.get_stop_loss(entry, sig, self.symbol)
                        tp = self.strategy.get_take_profit(entry, sig, self.symbol)
                    except TypeError:
                        sl = self.strategy.get_stop_loss(entry, sig)
                        tp = self.strategy.get_take_profit(entry, sig)
                    current = SimpleTrade(et, sig, entry, sl, tp)

        # EOD 強平
        if current is not None:
            last = df_1h.iloc[-1]
            current.close(df_1h.index[-1], float(last['close']), 'EOD',
                          self.position_usdt, self.leverage)
            capital += current.pnl_usdt
            trades.append(current)

        return self._calc_metrics(trades, capital, equity_curve, df_1h)

    def _calc_metrics(self, trades, final_cap, curve, df) -> dict:
        if not trades:
            return {
                'start_time': df.index[WARMUP_BARS], 'end_time': df.index[-1],
                'total_trades': 0, 'final_capital': self.initial_capital,
                'total_return_pct': 0.0, 'win_rate_pct': 0.0,
                'avg_win_usdt': 0.0, 'avg_loss_usdt': 0.0,
                'max_drawdown_pct': 0.0, 'profit_factor': 0.0, 'sharpe_ratio': 0.0,
                'trades': [],
            }

        wins   = [t for t in trades if t.pnl_usdt > 0]
        losses = [t for t in trades if t.pnl_usdt <= 0]
        win_rate = len(wins) / len(trades) * 100
        avg_win  = float(np.mean([t.pnl_usdt for t in wins]))   if wins   else 0.0
        avg_loss = float(np.mean([t.pnl_usdt for t in losses])) if losses else 0.0
        total_w  = sum(t.pnl_usdt for t in wins)
        total_l  = abs(sum(t.pnl_usdt for t in losses))
        pf = total_w / total_l if total_l > 0 else float('inf')

        peak = self.initial_capital
        max_dd = 0.0
        for _, b in curve:
            if b > peak: peak = b
            dd = (peak - b) / peak * 100
            if dd > max_dd: max_dd = dd

        ret = np.array([t.pnl_usdt for t in trades])
        sr = 0.0
        if len(ret) > 1 and ret.std(ddof=1) > 0:
            sr = float(ret.mean() / ret.std(ddof=1) * math.sqrt(len(ret)))

        return {
            'start_time': trades[0].entry_time, 'end_time': trades[-1].exit_time,
            'total_trades': len(trades), 'winning_trades': len(wins),
            'losing_trades': len(losses), 'final_capital': round(final_cap, 2),
            'total_return_pct': round((final_cap - self.initial_capital) / self.initial_capital * 100, 2),
            'win_rate_pct': round(win_rate, 2),
            'avg_win_usdt':  round(avg_win, 2),
            'avg_loss_usdt': round(avg_loss, 2),
            'max_drawdown_pct': round(max_dd, 2),
            'profit_factor': round(pf, 2) if pf != float('inf') else 'inf',
            'sharpe_ratio': round(sr, 2),
            'trades': [t.to_dict() for t in trades],
        }

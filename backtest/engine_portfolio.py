"""
TITAN — Portfolio-Level Backtest Engine（RICK 修正版）

對所有幣同步走時間軸，每根 1H K 棒：
  1. 檢查現有持倉 SL/TP
  2. 風控檢查（停機、冷卻、回撤）
  3. 收集所有幣的訊號 + score
  4. 排序，按 score 開最高的 N 筆（受 max_concurrent 限制）

風控規則：
  - 同時持倉上限：max_concurrent_positions
  - 總名義曝險上限：max_total_exposure_usdt
  - 單筆 SL_distance 上限（策略已過濾，這裡再檢一次）
  - 每日虧損熔斷：daily_loss_limit
  - 連虧 N 筆冷卻：consec_loss_cooldown
  - 帳戶回撤 -10% 降頻、-20% 停機
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd


TAKER_FEE = 0.0005
WARMUP_BARS = 60


class PortfolioTrade:
    __slots__ = ('symbol', 'entry_time', 'exit_time', 'side',
                 'entry_price', 'exit_price', 'sl_price', 'tp_price',
                 'pnl_pct', 'pnl_usdt', 'exit_reason', 'score')

    def __init__(self, symbol, entry_time, side, entry, sl, tp, score=0.0):
        self.symbol = symbol
        self.entry_time = entry_time
        self.exit_time  = None
        self.side       = side
        self.entry_price = entry
        self.exit_price = None
        self.sl_price   = sl
        self.tp_price   = tp
        self.pnl_pct    = 0.0
        self.pnl_usdt   = 0.0
        self.exit_reason = ''
        self.score = score

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
            'symbol': self.symbol,
            'entry_time': self.entry_time, 'exit_time': self.exit_time,
            'side': self.side,
            'entry_price': self.entry_price, 'exit_price': self.exit_price,
            'pnl_pct': round(self.pnl_pct, 4),
            'pnl_usdt': round(self.pnl_usdt, 2),
            'exit_reason': self.exit_reason,
            'score': round(self.score, 3),
        }


class PortfolioBacktestEngine:
    """投資組合層級回測引擎（含完整風控）"""

    def __init__(self, strategy_factory, settings: dict, risk_cfg: dict | None = None):
        """
        strategy_factory: 函式 () -> strategy instance（每幣一個獨立 instance）
        risk_cfg: 風控設定，可覆寫預設
        """
        self.strategy_factory = strategy_factory
        self.settings = settings

        cap = settings.get('capital', {})
        risk = settings.get('risk', {})
        self.initial_capital = float(cap.get('total_usdt', 5000))
        self.position_usdt   = float(cap.get('position_fixed_usdt', 100))
        self.leverage        = int(risk.get('leverage', 100))

        rc = risk_cfg or {}
        self.max_concurrent       = int(rc.get('max_concurrent_positions', 3))
        self.max_total_exposure   = float(rc.get('max_total_exposure_usdt', 30000))
        self.daily_loss_limit     = float(rc.get('daily_loss_limit_usdt', -250))
        self.consec_loss_threshold = int(rc.get('consec_loss_threshold', 3))
        self.cooldown_minutes     = int(rc.get('cooldown_minutes', 30))
        self.dd_throttle_pct      = float(rc.get('dd_throttle_pct', 10))   # -10% 降頻
        self.dd_stop_pct          = float(rc.get('dd_stop_pct', 20))       # -20% 停機

    def run(self, symbols_data: dict) -> dict:
        """
        symbols_data: dict[symbol -> {df_1h, df_4h, df_1d}]
        所有 df_1h 應該對齊（同樣的時間軸）
        """
        # 為每幣建立獨立策略 instance
        strategies = {sym: self.strategy_factory() for sym in symbols_data}

        # 統一時間軸（取 BTC 的 1H 為主）
        ref_symbol = list(symbols_data.keys())[0]
        timeline = symbols_data[ref_symbol]['df_1h'].index

        # 狀態
        capital = self.initial_capital
        peak_balance = capital
        positions: dict[str, PortfolioTrade] = {}
        all_trades = []
        equity_curve = []

        daily_pnl = 0.0
        current_day = None
        consec_losses = 0
        cooldown_until = None
        stopped = False

        for t_idx in range(WARMUP_BARS, len(timeline)):
            if stopped:
                break

            t = timeline[t_idx]

            # 換日重置 daily_pnl
            day = t.date() if hasattr(t, 'date') else None
            if day != current_day:
                current_day = day
                daily_pnl = 0.0

            # 1. 檢查持倉 SL/TP
            for sym in list(positions.keys()):
                pos = positions[sym]
                df_1h = symbols_data[sym]['df_1h']
                if t not in df_1h.index:
                    continue
                bar = df_1h.loc[t]

                if pos.side == 'LONG':
                    hit_sl = bar['low']  <= pos.sl_price
                    hit_tp = bar['high'] >= pos.tp_price
                else:
                    hit_sl = bar['high'] >= pos.sl_price
                    hit_tp = bar['low']  <= pos.tp_price

                if hit_sl or hit_tp:
                    price  = pos.sl_price if hit_sl else pos.tp_price
                    reason = 'SL' if hit_sl else 'TP'
                    pos.close(t, price, reason, self.position_usdt, self.leverage)
                    capital += pos.pnl_usdt
                    daily_pnl += pos.pnl_usdt
                    all_trades.append(pos)
                    del positions[sym]

                    if pos.pnl_usdt < 0:
                        consec_losses += 1
                        if consec_losses >= self.consec_loss_threshold:
                            cooldown_until = t + timedelta(minutes=self.cooldown_minutes)
                    else:
                        consec_losses = 0

                    if capital > peak_balance:
                        peak_balance = capital

            # 2. 風控檢查
            current_dd_pct = (peak_balance - capital) / peak_balance * 100 if peak_balance > 0 else 0

            # -20% 停機
            if current_dd_pct >= self.dd_stop_pct:
                stopped = True
                continue

            # 每日熔斷
            if daily_pnl <= self.daily_loss_limit:
                continue

            # 連虧冷卻
            if cooldown_until is not None and t < cooldown_until:
                continue
            elif cooldown_until is not None and t >= cooldown_until:
                cooldown_until = None
                consec_losses = 0  # 冷卻完重置

            # 持倉上限
            if len(positions) >= self.max_concurrent:
                continue

            # -10% 降頻：max_concurrent 砍半
            effective_max = self.max_concurrent
            if current_dd_pct >= self.dd_throttle_pct:
                effective_max = max(1, self.max_concurrent // 2)
            if len(positions) >= effective_max:
                continue

            # 3. 收集所有訊號（不在持倉中的幣）
            candidates = []
            for sym, data in symbols_data.items():
                if sym in positions:
                    continue
                df_1h_full = data['df_1h']
                if t not in df_1h_full.index:
                    continue
                idx = df_1h_full.index.get_loc(t)
                sub_1h = df_1h_full.iloc[:idx + 1]

                df_4h = data.get('df_4h')
                df_1d = data.get('df_1d')
                sub_4h = df_4h[df_4h.index < t] if df_4h is not None else None
                sub_1d = df_1d[df_1d.index < t] if df_1d is not None else None

                strat = strategies[sym]
                if hasattr(strat, 'update_data'):
                    strat.update_data(sub_4h, sub_1d)

                # 取訊號 + score
                if hasattr(strat, 'calculate_signal_with_score'):
                    sig, score, sl_dist = strat.calculate_signal_with_score(sub_1h, sym)
                else:
                    sig = strat.calculate_signals(sub_1h, sym)
                    score, sl_dist = (0.5, 0.005) if sig in ('LONG', 'SHORT') else (0.0, 0.0)

                if sig in ('LONG', 'SHORT'):
                    candidates.append((sym, sig, score, sl_dist))

            # 4. 按 score 排序，取 top N
            candidates.sort(key=lambda x: x[2], reverse=True)
            slots = effective_max - len(positions)

            for sym, sig, score, sl_dist in candidates[:slots]:
                # 檢查總曝險
                current_exposure = len(positions) * (self.position_usdt * self.leverage)
                new_exposure = current_exposure + (self.position_usdt * self.leverage)
                if new_exposure > self.max_total_exposure:
                    break

                # 在 t+1 開倉
                df_1h_full = symbols_data[sym]['df_1h']
                idx = df_1h_full.index.get_loc(t)
                if idx + 1 >= len(df_1h_full):
                    continue
                next_bar = df_1h_full.iloc[idx + 1]
                entry_t = df_1h_full.index[idx + 1]
                entry = float(next_bar['open'])

                strat = strategies[sym]
                sl = strat.get_stop_loss(entry, sig, sym)
                tp = strat.get_take_profit(entry, sig, sym)

                positions[sym] = PortfolioTrade(sym, entry_t, sig, entry, sl, tp, score)

            equity_curve.append((t, capital))

        # 結束強制平倉
        for sym, pos in list(positions.items()):
            df_1h = symbols_data[sym]['df_1h']
            last_bar = df_1h.iloc[-1]
            pos.close(df_1h.index[-1], float(last_bar['close']), 'EOD',
                      self.position_usdt, self.leverage)
            capital += pos.pnl_usdt
            all_trades.append(pos)

        return self._calc_metrics(all_trades, capital, peak_balance, equity_curve)

    def _calc_metrics(self, trades, final_cap, peak, curve) -> dict:
        if not trades:
            return {
                'total_trades': 0, 'final_capital': self.initial_capital,
                'total_return_pct': 0.0, 'win_rate_pct': 0.0,
                'avg_win_usdt': 0.0, 'avg_loss_usdt': 0.0,
                'max_drawdown_pct': 0.0, 'profit_factor': 0.0, 'sharpe_ratio': 0.0,
                'trades': [], 'by_symbol': {},
            }

        wins   = [t for t in trades if t.pnl_usdt > 0]
        losses = [t for t in trades if t.pnl_usdt <= 0]
        win_rate = len(wins) / len(trades) * 100
        avg_win  = float(np.mean([t.pnl_usdt for t in wins]))   if wins   else 0.0
        avg_loss = float(np.mean([t.pnl_usdt for t in losses])) if losses else 0.0
        total_w  = sum(t.pnl_usdt for t in wins)
        total_l  = abs(sum(t.pnl_usdt for t in losses))
        pf = total_w / total_l if total_l > 0 else float('inf')

        # Max DD（從 equity curve）
        max_dd = 0.0
        peak_running = self.initial_capital
        for _, b in curve:
            if b > peak_running:
                peak_running = b
            dd = (peak_running - b) / peak_running * 100
            if dd > max_dd:
                max_dd = dd

        ret = np.array([t.pnl_usdt for t in trades])
        sr = 0.0
        if len(ret) > 1 and ret.std(ddof=1) > 0:
            sr = float(ret.mean() / ret.std(ddof=1) * math.sqrt(len(ret)))

        # 按幣分類
        by_symbol = {}
        for t in trades:
            sym = t.symbol
            if sym not in by_symbol:
                by_symbol[sym] = {'trades': 0, 'wins': 0, 'pnl': 0.0}
            by_symbol[sym]['trades'] += 1
            by_symbol[sym]['pnl']    += t.pnl_usdt
            if t.pnl_usdt > 0:
                by_symbol[sym]['wins'] += 1

        return {
            'total_trades': len(trades),
            'winning_trades': len(wins), 'losing_trades': len(losses),
            'final_capital': round(final_cap, 2),
            'peak_balance': round(peak, 2),
            'total_return_pct': round((final_cap - self.initial_capital) / self.initial_capital * 100, 2),
            'win_rate_pct': round(win_rate, 2),
            'avg_win_usdt': round(avg_win, 2),
            'avg_loss_usdt': round(avg_loss, 2),
            'max_drawdown_pct': round(max_dd, 2),
            'profit_factor': round(pf, 2) if pf != float('inf') else 'inf',
            'sharpe_ratio': round(sr, 2),
            'by_symbol': by_symbol,
            'trades': [t.to_dict() for t in trades],
        }

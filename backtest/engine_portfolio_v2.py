"""
TITAN — Portfolio Engine V2 (RICK V2 Aggressive Passive 規格)

特性：
  - Active / Shadow List：Active 才開倉，Shadow 只記錄訊號
  - 完整 portfolio 風控（每日/每週/連虧/回撤分級停機）
  - 滑價模擬（taker 0.05% × 2 + slippage 0.05%）
  - 資金費率（每 8 小時 0.01% 持倉成本）
  - 爆倉模擬（餘額 < 0 立刻全停）
  - 單筆風險預算（SL loss + 手續費 + 滑價 + funding）
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd


TAKER_FEE = 0.0005       # 0.05% 單邊
SLIPPAGE  = 0.0005       # 0.05% 滑價（每筆）
FUNDING_8H = 0.0001      # 0.01% 每 8 小時
WARMUP_BARS = 60


class V2Trade:
    __slots__ = ('symbol', 'is_shadow', 'entry_time', 'exit_time', 'side',
                 'entry_price', 'exit_price', 'sl_price', 'tp_price',
                 'pnl_pct', 'pnl_usdt', 'exit_reason', 'score',
                 'leverage', 'margin_usdt', 'sl_distance')

    def __init__(self, symbol, is_shadow, entry_time, side, entry, sl, tp,
                 score, leverage, margin, sl_distance):
        self.symbol = symbol
        self.is_shadow = is_shadow
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
        self.leverage = leverage
        self.margin_usdt = margin
        self.sl_distance = sl_distance

    def close(self, t, price, reason):
        self.exit_time   = t
        self.exit_price  = price
        self.exit_reason = reason
        if self.side == 'LONG':
            raw = (price - self.entry_price) / self.entry_price
        else:
            raw = (self.entry_price - price) / self.entry_price

        # Fees and slippage are paid on notional. Because pnl_usdt is
        # margin_usdt * net, convert them to margin-return terms.
        round_trip_cost = (TAKER_FEE * 2 + SLIPPAGE * 2) * self.leverage
        net = raw * self.leverage - round_trip_cost

        # 資金費率：每持倉 8 小時扣 0.01%
        if self.exit_time is not None:
            hours = (self.exit_time - self.entry_time).total_seconds() / 3600
            funding_periods = hours / 8
            net -= FUNDING_8H * self.leverage * funding_periods

        self.pnl_pct  = net * 100
        self.pnl_usdt = self.margin_usdt * net

    def worst_case_loss(self):
        """估算單筆最壞損失（給風險預算用）"""
        # SL 觸發 + 手續費 + 滑價 + 1 個 funding 週期
        loss_pct = (
            self.sl_distance * self.leverage
            + (TAKER_FEE * 2 + SLIPPAGE * 2) * self.leverage
            + FUNDING_8H * self.leverage
        )
        return self.margin_usdt * loss_pct

    def to_dict(self):
        return {
            'symbol': self.symbol, 'is_shadow': self.is_shadow,
            'entry_time': self.entry_time, 'exit_time': self.exit_time,
            'side': self.side,
            'entry_price': self.entry_price, 'exit_price': self.exit_price,
            'sl_price': self.sl_price, 'tp_price': self.tp_price,
            'leverage': self.leverage, 'margin_usdt': self.margin_usdt,
            'pnl_pct': round(self.pnl_pct, 4),
            'pnl_usdt': round(self.pnl_usdt, 2),
            'exit_reason': self.exit_reason,
            'score': round(self.score, 3),
        }


class PortfolioEngineV2:
    """V2 Aggressive Passive Portfolio Engine"""

    DEFAULT_RISK = {
        'max_concurrent_positions': 2,
        'extra_position_risk_cap_usdt': 400,  # 第 3 筆只在已開倉風險 ≤ 400U 時允許
        'max_total_exposure_usdt':  30000,
        'max_open_risk_pct':        8.0,      # 總未平倉 worst-case loss ≤ 帳戶 8%
        'daily_loss_limit_usdt':    -250,
        'weekly_loss_limit_usdt':   -600,
        'consec_loss_threshold':    3,
        'cooldown_hours':           6,
        'dd_pause_pct':             20,        # -20% 停止開新倉
        'dd_stop_pct':              30,        # -30% 全停
    }

    def __init__(self, strategy_factory, settings: dict,
                 active_list: list, shadow_list: list = None,
                 risk_cfg: dict = None):
        self.strategy_factory = strategy_factory
        self.settings = settings
        self.active_list = set(active_list)
        self.shadow_list = set(shadow_list or [])

        cap = settings.get('capital', {})
        risk = settings.get('risk', {})
        self.initial_capital = float(cap.get('total_usdt', 5000))
        self.position_usdt   = float(cap.get('position_fixed_usdt', 100))
        self.leverage        = int(risk.get('leverage', 100))

        self.rc = {**self.DEFAULT_RISK, **(risk_cfg or {})}

    def run(self, symbols_data: dict) -> dict:
        # 為每幣建立獨立策略 instance
        all_symbols = list(symbols_data.keys())
        strategies = {sym: self.strategy_factory() for sym in all_symbols}

        ref_symbol = all_symbols[0]
        timeline = symbols_data[ref_symbol]['df_1h'].index

        capital = self.initial_capital
        peak_balance = capital
        active_positions: dict[str, V2Trade] = {}   # Active 真實持倉
        shadow_positions: dict[str, V2Trade] = {}   # Shadow 模擬持倉
        all_trades = []
        equity_curve = []

        daily_pnl = 0.0
        weekly_pnl = 0.0
        current_day = None
        current_week = None
        consec_losses = 0
        cooldown_until = None
        new_orders_blocked = False  # -20% 停新倉
        fully_stopped = False        # -30% 全停 / 爆倉

        for t_idx in range(WARMUP_BARS, len(timeline)):
            if fully_stopped:
                break

            t = timeline[t_idx]

            # 換日 / 換週重置
            day = t.date() if hasattr(t, 'date') else None
            if day != current_day:
                current_day = day
                daily_pnl = 0.0
            iso_week = t.isocalendar()[:2] if hasattr(t, 'isocalendar') else None
            if iso_week != current_week:
                current_week = iso_week
                weekly_pnl = 0.0

            # 1. 檢查所有持倉的 SL/TP（包含 shadow）
            for sym in list(active_positions.keys()) + list(shadow_positions.keys()):
                pos_dict = active_positions if sym in active_positions else shadow_positions
                pos = pos_dict[sym]
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
                    pos.close(t, price, reason)
                    all_trades.append(pos)

                    if pos.is_shadow:
                        del shadow_positions[sym]
                    else:
                        capital += pos.pnl_usdt
                        daily_pnl += pos.pnl_usdt
                        weekly_pnl += pos.pnl_usdt
                        del active_positions[sym]

                        if pos.pnl_usdt < 0:
                            consec_losses += 1
                            if consec_losses >= self.rc['consec_loss_threshold']:
                                cooldown_until = t + timedelta(hours=self.rc['cooldown_hours'])
                        else:
                            consec_losses = 0

                        if capital > peak_balance:
                            peak_balance = capital

                        # 爆倉檢查
                        if capital <= 0:
                            fully_stopped = True
                            break

            if fully_stopped:
                break

            # 2. 風控檢查（Active 開新倉用）
            current_dd_pct = (peak_balance - capital) / peak_balance * 100 if peak_balance > 0 else 0

            if current_dd_pct >= self.rc['dd_stop_pct']:
                fully_stopped = True
                continue

            new_orders_blocked = current_dd_pct >= self.rc['dd_pause_pct']

            can_open_active = (
                not new_orders_blocked
                and daily_pnl > self.rc['daily_loss_limit_usdt']
                and weekly_pnl > self.rc['weekly_loss_limit_usdt']
                and (cooldown_until is None or t >= cooldown_until)
            )
            if cooldown_until is not None and t >= cooldown_until:
                cooldown_until = None
                consec_losses = 0

            market_context = self._build_market_context(symbols_data, t)

            # 3. 收集所有訊號
            candidates = []
            for sym in all_symbols:
                if sym in active_positions or sym in shadow_positions:
                    continue
                df_1h_full = symbols_data[sym]['df_1h']
                if t not in df_1h_full.index:
                    continue
                idx = df_1h_full.index.get_loc(t)
                sub_1h = df_1h_full.iloc[:idx + 1]

                df_4h = symbols_data[sym].get('df_4h')
                df_1d = symbols_data[sym].get('df_1d')
                sub_4h = df_4h[df_4h.index < t] if df_4h is not None else None
                sub_1d = df_1d[df_1d.index < t] if df_1d is not None else None

                strat = strategies[sym]
                if hasattr(strat, 'update_data'):
                    strat.update_data(sub_4h, sub_1d)
                if hasattr(strat, 'update_market_context'):
                    strat.update_market_context(market_context, sym)

                if hasattr(strat, 'calculate_signal_with_score'):
                    sig, score, sl_dist = strat.calculate_signal_with_score(sub_1h, sym)
                else:
                    sig = strat.calculate_signals(sub_1h, sym)
                    score, sl_dist = (0.5, 0.005) if sig in ('LONG', 'SHORT') else (0.0, 0.0)

                if sig in ('LONG', 'SHORT'):
                    candidates.append((sym, sig, score, sl_dist))

            # 4. 按 score 排序
            candidates.sort(key=lambda x: x[2], reverse=True)

            # 5. 處理 Active List 開倉（受風控）
            if can_open_active:
                for sym, sig, score, sl_dist in candidates:
                    if sym not in self.active_list:
                        continue

                    # 檢查同時持倉上限
                    n_open = len(active_positions)
                    if n_open >= 3:  # 絕對上限
                        break

                    # 計算當前未平倉風險
                    current_risk = sum(p.worst_case_loss() for p in active_positions.values())

                    # 第 3 筆額外限制：總未平倉風險 ≤ 400U
                    if n_open >= self.rc['max_concurrent_positions']:
                        if current_risk > self.rc['extra_position_risk_cap_usdt']:
                            continue  # 不開第 3 筆

                    # 檢查總曝險上限
                    new_exposure = (n_open + 1) * self.position_usdt * self.leverage
                    if new_exposure > self.rc['max_total_exposure_usdt']:
                        break

                    # 檢查總風險預算（worst case loss ≤ 帳戶 8%）
                    new_trade_loss = self.position_usdt * (
                        sl_dist * self.leverage
                        + (TAKER_FEE * 2 + SLIPPAGE * 2) * self.leverage
                        + FUNDING_8H * self.leverage
                    )
                    total_risk_usdt = current_risk + new_trade_loss
                    if total_risk_usdt > capital * self.rc['max_open_risk_pct'] / 100:
                        continue

                    # 通過，下一根開倉
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

                    active_positions[sym] = V2Trade(
                        sym, False, entry_t, sig, entry, sl, tp,
                        score, self.leverage, self.margin_usdt(sym),
                        sl_dist
                    )

            # 6. 處理 Shadow List（無風控、不影響資金）
            for sym, sig, score, sl_dist in candidates:
                if sym not in self.shadow_list:
                    continue
                if sym in shadow_positions:
                    continue

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

                shadow_positions[sym] = V2Trade(
                    sym, True, entry_t, sig, entry, sl, tp,
                    score, self.leverage, self.margin_usdt(sym),
                    sl_dist
                )

            equity_curve.append((t, capital))

        # EOD 強制平倉所有 active
        for sym, pos in list(active_positions.items()):
            df_1h = symbols_data[sym]['df_1h']
            last_bar = df_1h.iloc[-1]
            pos.close(df_1h.index[-1], float(last_bar['close']), 'EOD')
            capital += pos.pnl_usdt
            all_trades.append(pos)
        for sym, pos in list(shadow_positions.items()):
            df_1h = symbols_data[sym]['df_1h']
            last_bar = df_1h.iloc[-1]
            pos.close(df_1h.index[-1], float(last_bar['close']), 'EOD')
            all_trades.append(pos)

        return self._calc_metrics(all_trades, capital, peak_balance,
                                  equity_curve, fully_stopped)

    def _build_market_context(self, symbols_data: dict, t) -> dict:
        """Build cross-symbol regime/ranking context for rotation strategies."""
        rows = []
        btc_ret_7d = 0.0

        for sym, data in symbols_data.items():
            df = data.get('df_1h')
            if df is None or t not in df.index:
                continue
            idx = df.index.get_loc(t)
            if not isinstance(idx, int) or idx < 30:
                continue

            # Keep context aligned with strategies that use df.iloc[-2].
            i = max(0, idx - 1)
            close_now = float(df['close'].iloc[i])
            if close_now <= 0:
                continue

            i_7d = max(0, i - 24 * 7)
            i_30d = max(0, i - 24 * 30)
            ret_7d = close_now / float(df['close'].iloc[i_7d]) - 1 if i > i_7d else 0.0
            ret_30d = close_now / float(df['close'].iloc[i_30d]) - 1 if i > i_30d else 0.0

            vol_recent = float(df['volume'].iloc[max(0, i - 23):i + 1].sum())
            vol_prev = float(df['volume'].iloc[max(0, i - 47):max(0, i - 23)].sum())
            vol_ratio = vol_recent / vol_prev if vol_prev > 0 else 1.0

            rows.append({
                'symbol': sym,
                'ret_7d': ret_7d,
                'ret_30d': ret_30d,
                'vol_ratio': vol_ratio,
            })
            if sym.startswith('BTC/'):
                btc_ret_7d = ret_7d

        if not rows:
            return {'regime': 'NEUTRAL', 'scores': {}}

        m = pd.DataFrame(rows).set_index('symbol')
        m['rel_btc'] = m['ret_7d'] - btc_ret_7d

        long_score = (
            m['ret_30d'].rank(pct=True) * 0.30
            + m['ret_7d'].rank(pct=True) * 0.30
            + m['vol_ratio'].rank(pct=True) * 0.20
            + m['rel_btc'].rank(pct=True) * 0.20
        )
        short_score = (
            (-m['ret_30d']).rank(pct=True) * 0.30
            + (-m['ret_7d']).rank(pct=True) * 0.30
            + m['vol_ratio'].rank(pct=True) * 0.20
            + (-m['rel_btc']).rank(pct=True) * 0.20
        )

        long_order = list(long_score.sort_values(ascending=False).index)
        short_order = list(short_score.sort_values(ascending=False).index)
        scores = {}
        for sym in m.index:
            scores[sym] = {
                'long_score': float(long_score.loc[sym]),
                'short_score': float(short_score.loc[sym]),
                'long_rank': long_order.index(sym) + 1,
                'short_rank': short_order.index(sym) + 1,
                'ret_7d': float(m.loc[sym, 'ret_7d']),
                'ret_30d': float(m.loc[sym, 'ret_30d']),
                'vol_ratio': float(m.loc[sym, 'vol_ratio']),
                'rel_btc': float(m.loc[sym, 'rel_btc']),
            }

        btc_trend = self._major_trend(symbols_data, 'BTC/USDT:USDT', t)
        eth_trend = self._major_trend(symbols_data, 'ETH/USDT:USDT', t)
        if btc_trend == 'UP' and eth_trend == 'UP':
            regime = 'LONG'
        elif btc_trend == 'DOWN' and eth_trend == 'DOWN':
            regime = 'SHORT'
        else:
            regime = 'NEUTRAL'

        return {'regime': regime, 'scores': scores}

    def _major_trend(self, symbols_data: dict, symbol: str, t) -> str:
        data = symbols_data.get(symbol)
        if not data:
            return 'NEUTRAL'
        df_4h = data.get('df_4h')
        if df_4h is None:
            return 'NEUTRAL'
        sub = df_4h[df_4h.index < t]
        if len(sub) < 55:
            return 'NEUTRAL'
        ema20 = sub['close'].ewm(span=20, adjust=False).mean()
        ema50 = sub['close'].ewm(span=50, adjust=False).mean()
        if pd.isna(ema20.iloc[-1]) or pd.isna(ema50.iloc[-1]):
            return 'NEUTRAL'
        return 'UP' if ema20.iloc[-1] > ema50.iloc[-1] else 'DOWN'

    def margin_usdt(self, symbol):
        """單幣的保證金（預設一致）"""
        return self.position_usdt

    def _calc_metrics(self, trades, final_cap, peak, curve, stopped) -> dict:
        active = [t for t in trades if not t.is_shadow]
        shadow = [t for t in trades if t.is_shadow]

        def calc_one(ts, capital_used=True):
            if not ts:
                return {'total_trades': 0, 'win_rate_pct': 0.0,
                        'total_pnl_usdt': 0.0, 'total_return_pct': 0.0,
                        'avg_win_usdt': 0.0, 'avg_loss_usdt': 0.0,
                        'max_drawdown_pct': 0.0, 'profit_factor': 0.0,
                        'sharpe_ratio': 0.0}
            wins   = [t for t in ts if t.pnl_usdt > 0]
            losses = [t for t in ts if t.pnl_usdt <= 0]
            wr = len(wins) / len(ts) * 100
            avg_w = float(np.mean([t.pnl_usdt for t in wins])) if wins else 0
            avg_l = float(np.mean([t.pnl_usdt for t in losses])) if losses else 0
            tw = sum(t.pnl_usdt for t in wins)
            tl = abs(sum(t.pnl_usdt for t in losses))
            pf = tw / tl if tl > 0 else float('inf')
            tot = sum(t.pnl_usdt for t in ts)
            ret = np.array([t.pnl_usdt for t in ts])
            sr = float(ret.mean() / ret.std(ddof=1) * math.sqrt(len(ret))) \
                 if len(ret) > 1 and ret.std(ddof=1) > 0 else 0.0
            return {
                'total_trades': len(ts),
                'win_rate_pct': round(wr, 2),
                'total_pnl_usdt': round(tot, 2),
                'total_return_pct': round(tot / self.initial_capital * 100, 2) if capital_used else round(tot / self.initial_capital * 100, 2),
                'avg_win_usdt': round(avg_w, 2),
                'avg_loss_usdt': round(avg_l, 2),
                'profit_factor': round(pf, 2) if pf != float('inf') else 'inf',
                'sharpe_ratio': round(sr, 2),
            }

        # 計算 max DD（從 equity curve）
        peak_running = self.initial_capital
        max_dd = 0.0
        for _, b in curve:
            if b > peak_running: peak_running = b
            dd = (peak_running - b) / peak_running * 100
            if dd > max_dd: max_dd = dd

        active_metrics = calc_one(active)
        shadow_metrics = calc_one(shadow, capital_used=False)

        return {
            'fully_stopped':   stopped,
            'final_capital':   round(final_cap, 2),
            'peak_balance':    round(peak, 2),
            'max_drawdown_pct': round(max_dd, 2),
            'active': active_metrics,
            'shadow': shadow_metrics,
            'total_trades': len(trades),
            'active_count': len(active),
            'shadow_count': len(shadow),
            'by_symbol': self._by_symbol(trades),
            'trades': [t.to_dict() for t in trades],
        }

    def _by_symbol(self, trades):
        out = {}
        for t in trades:
            sym = t.symbol
            if sym not in out:
                out[sym] = {'is_shadow': t.is_shadow, 'trades': 0, 'wins': 0, 'pnl': 0.0}
            out[sym]['trades'] += 1
            out[sym]['pnl'] += t.pnl_usdt
            if t.pnl_usdt > 0:
                out[sym]['wins'] += 1
        return out

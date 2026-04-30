"""
TITAN v1 — 事件驅動回測引擎
規則：
  - 從第 50 根 K 線開始（前 49 根用來暖機指標）
  - 每根 K 線依截至當根的數據產生訊號（嚴禁 lookahead bias）
  - 訊號在「下一根 K 線開盤價」成交
  - 止損 / 止盈以當根 high / low 判斷是否觸發
  - 每筆進出場各收 0.05% taker fee
"""

import math
from typing import Optional

import numpy as np
import pandas as pd


# 手續費（單邊）
TAKER_FEE = 0.0005  # 0.05%
WARMUP_BARS = 50    # 前 N 根用來計算指標，不開倉


class Trade:
    """記錄單筆交易"""

    __slots__ = (
        'entry_time', 'exit_time',
        'side',
        'entry_price', 'exit_price',
        'pnl_pct', 'pnl_usdt',
        'exit_reason',
    )

    def __init__(self, entry_time, side: str, entry_price: float):
        self.entry_time  = entry_time
        self.exit_time   = None
        self.side        = side           # 'LONG' or 'SHORT'
        self.entry_price = entry_price
        self.exit_price  = None
        self.pnl_pct     = 0.0
        self.pnl_usdt    = 0.0
        self.exit_reason = ''            # 'SL' / 'TP' / 'EOD'

    def close(self, exit_time, exit_price: float, exit_reason: str,
              position_usdt: float, leverage: int):
        """平倉，計算損益"""
        self.exit_time  = exit_time
        self.exit_price = exit_price
        self.exit_reason = exit_reason

        # 毛報酬率（基於槓桿後名義倉位）
        if self.side == 'LONG':
            raw_pct = (exit_price - self.entry_price) / self.entry_price
        else:
            raw_pct = (self.entry_price - exit_price) / self.entry_price

        # 扣手續費（進場 + 出場）
        fee_pct = TAKER_FEE * 2
        net_pct = raw_pct * leverage - fee_pct

        self.pnl_pct  = net_pct * 100        # 轉成 %
        self.pnl_usdt = position_usdt * net_pct

    def to_dict(self) -> dict:
        return {
            'entry_time':  self.entry_time,
            'exit_time':   self.exit_time,
            'side':        self.side,
            'entry_price': self.entry_price,
            'exit_price':  self.exit_price,
            'pnl_pct':     self.pnl_pct,
            'pnl_usdt':    self.pnl_usdt,
            'exit_reason': self.exit_reason,
        }


class BacktestEngine:
    """事件驅動回測引擎"""

    def __init__(self, strategy, settings: dict):
        """
        Parameters
        ----------
        strategy : 任何有 calculate_signals / get_stop_loss / get_take_profit 方法的物件
        settings : 對應 config/settings.yaml 結構的 dict
        """
        self.strategy = strategy
        self.settings = settings

        # 讀取設定
        risk    = settings.get('risk', {})
        capital = settings.get('capital', {})

        self.initial_capital    = float(capital.get('total_usdt', 5000))
        self.position_size_pct  = float(risk.get('position_size_pct', 10)) / 100
        self.leverage           = int(risk.get('leverage', 20))
        self.stop_loss_pct      = float(risk.get('stop_loss_pct', 1.5)) / 100
        self.take_profit_pct    = float(risk.get('take_profit_pct', 3.0)) / 100

    # ------------------------------------------------------------------
    # 公開介面
    # ------------------------------------------------------------------

    def run(self, df: pd.DataFrame) -> dict:
        """
        執行回測。

        Parameters
        ----------
        df : pd.DataFrame
            columns=[open, high, low, close, volume]，index=DatetimeIndex
            至少要有 WARMUP_BARS + 2 根

        Returns
        -------
        dict  績效結果，欄位見 _calc_metrics()
        """
        df = df.copy()
        # 保留時間索引為欄位，統一命名為 '_time'
        df.index.name = df.index.name or 'index'
        df = df.reset_index(drop=False)
        # 統一時間欄位名稱
        time_col = [c for c in df.columns if c not in ('open','high','low','close','volume')][0]
        df.rename(columns={time_col: '_time'}, inplace=True)
        n  = len(df)

        if n < WARMUP_BARS + 2:
            raise ValueError(f'K 線數量不足，需要至少 {WARMUP_BARS + 2} 根，實際 {n} 根')

        capital       = self.initial_capital
        trades: list[Trade] = []
        current_trade: Optional[Trade] = None
        stop_loss_price  = None
        take_profit_price = None

        for i in range(WARMUP_BARS, n):
            bar = df.iloc[i]

            # ── 1. 若有持倉 → 先檢查止損止盈（用本根 K 線 high/low）
            if current_trade is not None:
                sl = stop_loss_price
                tp = take_profit_price
                hit_sl = False
                hit_tp = False

                if current_trade.side == 'LONG':
                    hit_sl = bar['low']  <= sl
                    hit_tp = bar['high'] >= tp
                else:  # SHORT
                    hit_sl = bar['high'] >= sl
                    hit_tp = bar['low']  <= tp

                if hit_sl or hit_tp:
                    # 同時觸發時，保守起見以止損價成交
                    if hit_sl and hit_tp:
                        exit_price  = sl
                        exit_reason = 'SL'
                    elif hit_sl:
                        exit_price  = sl
                        exit_reason = 'SL'
                    else:
                        exit_price  = tp
                        exit_reason = 'TP'

                    position_usdt = capital * self.position_size_pct
                    current_trade.close(
                        exit_time    = bar['_time'],
                        exit_price   = exit_price,
                        exit_reason  = exit_reason,
                        position_usdt = position_usdt,
                        leverage      = self.leverage,
                    )
                    capital += current_trade.pnl_usdt
                    trades.append(current_trade)
                    current_trade    = None
                    stop_loss_price  = None
                    take_profit_price = None
                    continue  # 本根 K 線已平倉，不再開倉

            # ── 2. 若無持倉 → 用截至第 i 根（含）的數據計算訊號
            if current_trade is None:
                # 傳入截至本根的 df 給策略（防止 lookahead bias）
                sub_df = self._build_subdf(df, i)
                signal = self.strategy.calculate_signals(sub_df)

                if signal in ('LONG', 'SHORT') and i + 1 < n:
                    # 以下一根開盤價進場
                    next_bar     = df.iloc[i + 1]
                    entry_price  = float(next_bar['open'])
                    entry_time   = next_bar['_time']

                    stop_loss_price   = self.strategy.get_stop_loss(entry_price, signal)
                    take_profit_price = self.strategy.get_take_profit(entry_price, signal)

                    current_trade = Trade(entry_time, signal, entry_price)

        # ── 3. 回測結束：強制平倉（用最後一根收盤價）
        if current_trade is not None:
            last_bar      = df.iloc[-1]
            exit_price    = float(last_bar['close'])
            exit_time     = last_bar['_time']
            position_usdt = capital * self.position_size_pct

            current_trade.close(
                exit_time     = exit_time,
                exit_price    = exit_price,
                exit_reason   = 'EOD',
                position_usdt = position_usdt,
                leverage      = self.leverage,
            )
            capital += current_trade.pnl_usdt
            trades.append(current_trade)

        return self._calc_metrics(trades, capital, df)

    # ------------------------------------------------------------------
    # 內部方法
    # ------------------------------------------------------------------

    def _build_subdf(self, df: pd.DataFrame, upto: int) -> pd.DataFrame:
        """
        回傳截至第 upto 根（含）的 DataFrame，並將原始 index 還原。
        """
        sub = df.iloc[:upto + 1].copy()
        # 還原 DatetimeIndex 供策略使用
        sub = sub.set_index('_time')
        return sub[['open', 'high', 'low', 'close', 'volume']]

    def _calc_metrics(self, trades: list[Trade], final_capital: float, df: pd.DataFrame) -> dict:
        """計算並回傳所有績效指標"""

        total_trades  = len(trades)
        trade_list    = [t.to_dict() for t in trades]

        # 無交易的邊界處理
        if total_trades == 0:
            start_time = df.iloc[WARMUP_BARS]['_time']
            end_time   = df.iloc[-1]['_time']
            return {
                'start_time':       start_time,
                'end_time':         end_time,
                'total_return_pct': 0.0,
                'win_rate_pct':     0.0,
                'total_trades':     0,
                'winning_trades':   0,
                'losing_trades':    0,
                'avg_win_pct':      0.0,
                'avg_loss_pct':     0.0,
                'max_drawdown_pct': 0.0,
                'sharpe_ratio':     0.0,
                'trade_list':       [],
            }

        # 勝負分類
        wins   = [t for t in trades if t.pnl_usdt > 0]
        losses = [t for t in trades if t.pnl_usdt <= 0]

        win_rate_pct = len(wins) / total_trades * 100
        avg_win_pct  = float(np.mean([t.pnl_pct for t in wins]))   if wins   else 0.0
        avg_loss_pct = float(np.mean([t.pnl_pct for t in losses])) if losses else 0.0

        # 總報酬率
        total_return_pct = (final_capital - self.initial_capital) / self.initial_capital * 100

        # 最大回撤
        max_drawdown_pct = self._calc_max_drawdown(trades)

        # 夏普比率（簡化版：年化報酬 / 年化標準差）
        sharpe_ratio = self._calc_sharpe(trades)

        # 時間範圍
        start_time = trades[0].entry_time
        end_time   = trades[-1].exit_time

        return {
            'start_time':       start_time,
            'end_time':         end_time,
            'total_return_pct': round(total_return_pct, 4),
            'win_rate_pct':     round(win_rate_pct, 2),
            'total_trades':     total_trades,
            'winning_trades':   len(wins),
            'losing_trades':    len(losses),
            'avg_win_pct':      round(avg_win_pct, 4),
            'avg_loss_pct':     round(avg_loss_pct, 4),
            'max_drawdown_pct': round(max_drawdown_pct, 4),
            'sharpe_ratio':     round(sharpe_ratio, 4),
            'trade_list':       trade_list,
        }

    def _calc_max_drawdown(self, trades: list[Trade]) -> float:
        """計算最大回撤 %（基於累積資金曲線）"""
        equity = self.initial_capital
        peak   = equity
        max_dd = 0.0

        for t in trades:
            equity += t.pnl_usdt
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak * 100
            if dd > max_dd:
                max_dd = dd

        return max_dd

    def _calc_sharpe(self, trades: list[Trade]) -> float:
        """
        夏普比率（簡化版）
        = 年化報酬率 / 年化標準差
        以每筆交易的 pnl_pct 作為報酬序列，無風險利率 = 0
        """
        if len(trades) < 2:
            return 0.0

        returns = np.array([t.pnl_pct for t in trades])
        mean_r  = np.mean(returns)
        std_r   = np.std(returns, ddof=1)

        if std_r == 0:
            return 0.0

        # 假設每年約 252 個交易日，每日平均 4 筆交易 → 年化因子
        ann_factor = math.sqrt(252 * 4)
        return float(mean_r / std_r * ann_factor)

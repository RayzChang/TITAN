"""
EMA 交叉 + RSI 過濾策略 v1.1（SAM 優化版）

策略邏輯
--------
做多訊號：
  1. EMA 快線上穿慢線（Golden Cross）
  2. RSI 在 [rsi_oversold, rsi_overbought] 之間
  3. [趨勢濾網] 當前收盤價 > EMA-100（確認大方向向上）
  4. [成交量濾網] 本根成交量 > 均量 * volume_ratio_min

做空訊號：
  1. EMA 快線下穿慢線（Death Cross）
  2. RSI 在 [rsi_oversold, rsi_overbought] 之間
  3. [趨勢濾網] 當前收盤價 < EMA-100（確認大方向向下）
  4. [成交量濾網] 本根成交量 > 均量 * volume_ratio_min

V1.1 改動說明
-------------
- rsi_overbought: 70 → 65，rsi_oversold: 30 → 35（縮緊，減少追高追低）
- 新增趨勢濾網（EMA-100）：只順大趨勢方向交易，避免逆勢假突破
- 新增成交量濾網：量能不足代表突破不可信，跳過
- 以上三個濾網均可透過 settings.yaml 獨立開關

settings 結構
-------------
settings['strategy']['ema_crossover'] = {
    'fast_period'      : 9,
    'slow_period'      : 21,
    'rsi_period'       : 14,
    'rsi_overbought'   : 65,
    'rsi_oversold'     : 35,
    'trend_ema_period' : 100,
    'use_trend_filter' : True,
    'use_volume_filter': True,
    'volume_ma_period' : 20,
    'volume_ratio_min' : 1.2,
}
settings['risk'] = {
    'stop_loss_pct'  : 1.5,
    'take_profit_pct': 3.0,
}
"""
import pandas as pd
from .base_strategy import BaseStrategy
from indicators.technical import add_ema, add_rsi, add_atr, add_macd


class EMAcrossover(BaseStrategy):
    """
    EMA 交叉策略 v1.1，繼承自 BaseStrategy。
    新增趨勢濾網（EMA-100）與成交量濾網。

    Parameters
    ----------
    settings : dict
        包含 strategy.ema_crossover 與 risk 區塊的設定字典。
    """

    def __init__(self, settings: dict):
        strategy_cfg = settings['strategy']['ema_crossover']
        risk_cfg     = settings.get('risk', {})

        # EMA 交叉參數
        self.fast_period    = strategy_cfg.get('fast_period',    9)
        self.slow_period    = strategy_cfg.get('slow_period',   21)
        self.rsi_period     = strategy_cfg.get('rsi_period',    14)
        self.rsi_overbought = strategy_cfg.get('rsi_overbought', 65)
        self.rsi_oversold   = strategy_cfg.get('rsi_oversold',  35)

        # 趨勢濾網
        self.use_trend_filter  = strategy_cfg.get('use_trend_filter', True)
        self.trend_ema_period  = strategy_cfg.get('trend_ema_period', 100)

        # 成交量濾網
        self.use_volume_filter = strategy_cfg.get('use_volume_filter', True)
        self.volume_ma_period  = strategy_cfg.get('volume_ma_period',  20)
        self.volume_ratio_min  = strategy_cfg.get('volume_ratio_min',  1.2)

        # ATR 動態止損（V1.2）— 只放大不縮小，底板為固定百分比
        self.use_atr_sl        = strategy_cfg.get('use_atr_sl',        True)
        self.atr_period        = strategy_cfg.get('atr_period',          14)
        self.atr_sl_multiplier = strategy_cfg.get('atr_sl_multiplier', 1.5)
        self.atr_tp_multiplier = strategy_cfg.get('atr_tp_multiplier', 3.0)

        # MACD 動能確認（V1.2 MIA 決策）
        self.use_macd_confirm = strategy_cfg.get('use_macd_confirm', True)

        # 固定止損止盈（底板，ATR 不足時保底用）
        self.stop_loss_pct   = risk_cfg.get('stop_loss_pct',   1.5)
        self.take_profit_pct = risk_cfg.get('take_profit_pct', 3.0)

        # 欄位名稱（df 上使用）
        self._fast_col   = f'ema_{self.fast_period}'
        self._slow_col   = f'ema_{self.slow_period}'
        self._rsi_col    = f'rsi_{self.rsi_period}'
        self._trend_col  = f'ema_{self.trend_ema_period}'
        self._vol_ma_col = f'vol_ma_{self.volume_ma_period}'
        self._atr_col    = f'atr_{self.atr_period}'

        # 最新 ATR 值（由 calculate_signals 更新，供 get_stop_loss/get_take_profit 使用）
        self._last_atr: float = 0.0

    # ------------------------------------------------------------------
    # 私有：準備指標欄位
    # ------------------------------------------------------------------

    def _prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        """確保 df 中已有所需的指標欄位，不重複計算。"""
        df = df.copy()

        # 基礎指標
        if self._fast_col not in df.columns:
            df = add_ema(df, self.fast_period, self._fast_col)
        if self._slow_col not in df.columns:
            df = add_ema(df, self.slow_period, self._slow_col)
        if self._rsi_col not in df.columns:
            df = add_rsi(df, self.rsi_period)

        # 趨勢濾網 EMA
        if self.use_trend_filter and self._trend_col not in df.columns:
            df = add_ema(df, self.trend_ema_period, self._trend_col)

        # 成交量均線
        if self.use_volume_filter and self._vol_ma_col not in df.columns:
            df[self._vol_ma_col] = df['volume'].rolling(self.volume_ma_period).mean()

        # ATR
        if self.use_atr_sl and self._atr_col not in df.columns:
            df = add_atr(df, self.atr_period)

        # MACD
        if self.use_macd_confirm and 'macd_hist' not in df.columns:
            df = add_macd(df)

        return df

    # ------------------------------------------------------------------
    # BaseStrategy 抽象方法實作
    # ------------------------------------------------------------------

    def calculate_signals(self, df: pd.DataFrame) -> str:
        """
        分析最後兩根 K 線，回傳交易訊號。

        Returns
        -------
        str  'LONG' | 'SHORT' | 'HOLD'
        """
        df = self._prepare(df)

        # 確認基礎指標有足夠有效數據
        base_cols = [self._fast_col, self._slow_col, self._rsi_col]
        if self.use_trend_filter:
            base_cols.append(self._trend_col)

        valid = df[base_cols].dropna()
        if len(valid) < 2:
            return 'HOLD'

        prev = valid.iloc[-2]
        curr = valid.iloc[-1]

        # 取當前完整那一行（含 volume）
        curr_full = df.iloc[-1]

        prev_fast = prev[self._fast_col]
        prev_slow = prev[self._slow_col]
        curr_fast = curr[self._fast_col]
        curr_slow = curr[self._slow_col]
        curr_rsi  = curr[self._rsi_col]

        # ── 1. EMA 交叉判斷 ──
        bullish_cross = (prev_fast < prev_slow) and (curr_fast > curr_slow)
        bearish_cross = (prev_fast > prev_slow) and (curr_fast < curr_slow)

        if not (bullish_cross or bearish_cross):
            return 'HOLD'

        # ── 2. RSI 過濾（縮緊至 35-65）──
        rsi_ok = self.rsi_oversold < curr_rsi < self.rsi_overbought
        if not rsi_ok:
            return 'HOLD'

        # ── 3. 趨勢濾網（EMA-100）──
        if self.use_trend_filter:
            curr_price = float(curr_full['close'])
            curr_trend = float(curr[self._trend_col])
            if bullish_cross and curr_price < curr_trend:
                return 'HOLD'  # 價格在長期均線下方 → 多頭訊號不可信
            if bearish_cross and curr_price > curr_trend:
                return 'HOLD'  # 價格在長期均線上方 → 空頭訊號不可信

        # ── 4. 成交量濾網 ──
        if self.use_volume_filter:
            curr_vol    = float(curr_full['volume'])
            vol_ma_val  = df[self._vol_ma_col].iloc[-1]
            if pd.isna(vol_ma_val) or curr_vol < vol_ma_val * self.volume_ratio_min:
                return 'HOLD'  # 量能不足，突破不可信

        # ── 5. MACD 動能確認（方向需與交叉一致）──
        if self.use_macd_confirm and 'macd_hist' in df.columns:
            macd_hist_now  = df['macd_hist'].iloc[-1]
            macd_hist_prev = df['macd_hist'].iloc[-2] if len(df) >= 2 else macd_hist_now
            if pd.isna(macd_hist_now):
                pass  # 數據不足，跳過此濾網
            else:
                # 做多：MACD 柱狀圖為正（多方動能主導）且持續上升
                if bullish_cross and not (macd_hist_now > 0 or macd_hist_now > macd_hist_prev):
                    return 'HOLD'
                # 做空：MACD 柱狀圖為負（空方動能主導）且持續下降
                if bearish_cross and not (macd_hist_now < 0 or macd_hist_now < macd_hist_prev):
                    return 'HOLD'

        # ── 更新最新 ATR 值（供 get_stop_loss/get_take_profit 使用）──
        if self.use_atr_sl and self._atr_col in df.columns:
            atr_val = df[self._atr_col].iloc[-1]
            self._last_atr = float(atr_val) if not pd.isna(atr_val) else 0.0

        # ── 全部通過 → 出訊號 ──
        if bullish_cross:
            return 'LONG'
        else:
            return 'SHORT'

    @staticmethod
    def _smart_round(price: float) -> float:
        """
        根據價格量級決定精度，避免低價幣四捨五入導致止損止盈失效。
        例：$0.037 需要至少 5 位小數才能表達 1.5% 差異。
        """
        if price >= 1000:
            return round(price, 2)
        elif price >= 100:
            return round(price, 3)
        elif price >= 1:
            return round(price, 4)
        elif price >= 0.01:
            return round(price, 6)
        else:
            return round(price, 8)

    def get_stop_loss(self, entry_price: float, signal: str) -> float:
        """
        計算止損價位。
        ATR 模式：sl_dist = max(ATR × multiplier, entry × 1.5%)
        → 只放大不縮小，固定 1.5% 為底板
        """
        min_sl_dist = entry_price * (self.stop_loss_pct / 100)   # 1.5% 底板

        if self.use_atr_sl and self._last_atr > 0:
            atr_dist = self._last_atr * self.atr_sl_multiplier
            sl_dist  = max(atr_dist, min_sl_dist)                # 取較大值
        else:
            sl_dist = min_sl_dist

        if signal == 'LONG':
            return self._smart_round(entry_price - sl_dist)
        elif signal == 'SHORT':
            return self._smart_round(entry_price + sl_dist)
        else:
            return self._smart_round(entry_price)

    def get_take_profit(self, entry_price: float, signal: str) -> float:
        """
        計算止盈價位。
        ATR 模式：tp_dist = max(ATR × multiplier, entry × 3.0%)
        → 只放大不縮小，維持 2:1 R:R
        """
        min_tp_dist = entry_price * (self.take_profit_pct / 100)  # 3.0% 底板

        if self.use_atr_sl and self._last_atr > 0:
            atr_dist = self._last_atr * self.atr_tp_multiplier
            tp_dist  = max(atr_dist, min_tp_dist)                 # 取較大值
        else:
            tp_dist = min_tp_dist

        if signal == 'LONG':
            return self._smart_round(entry_price + tp_dist)
        elif signal == 'SHORT':
            return self._smart_round(entry_price - tp_dist)
        else:
            return self._smart_round(entry_price)

    def __repr__(self) -> str:
        filters = []
        if self.use_trend_filter:
            filters.append(f"TrendEMA{self.trend_ema_period}")
        if self.use_volume_filter:
            filters.append(f"Vol>{self.volume_ratio_min}x")
        filter_str = f" | Filters: {', '.join(filters)}" if filters else ""
        return (
            f"EMAcrossover v1.1("
            f"fast={self.fast_period}, slow={self.slow_period}, "
            f"rsi={self.rsi_period} [{self.rsi_oversold}-{self.rsi_overbought}], "
            f"SL={self.stop_loss_pct}%, TP={self.take_profit_pct}%"
            f"{filter_str})"
        )

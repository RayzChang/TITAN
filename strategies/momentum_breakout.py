"""
動量突破策略 v1.0（整合自 crypto-quant-trader ZIP）

策略邏輯（做多，不做空）
------------------------
1. 價格突破近 N 根 K 線最高點（動量確認）
2. 成交量 > 均量 × 2（量能確認）
3. RSI 在合理區間（避免追高超買）
4. MACD 金叉且 MACD > 0（趨勢動能向上）

與原版差異
----------
- RSI 改用 Wilder's Smoothing（EWM），與 TradingView 一致
- 風控全部交給 TITAN 的 RiskManager (SHIELD)，不在此類處理
- 繼承 BaseStrategy，相容 TITAN 主迴圈
- 止損改用 ATR 動態計算，1.5% 固定為底板

settings 結構
-------------
settings['strategy']['momentum_breakout'] = {
    'window_size'     : 20,     # 突破回望窗口（根 K 線數）
    'rsi_period'      : 14,
    'rsi_overbought'  : 70,     # RSI 超買上限
    'rsi_oversold'    : 40,     # RSI 有效下限（太低不進）
    'volume_ratio_min': 2.0,    # 成交量需高於均量幾倍
    'require_macd_cross': True, # 是否要求 MACD 金叉
    'use_atr_sl'      : True,
    'atr_period'      : 14,
    'atr_sl_multiplier': 1.5,
    'atr_tp_multiplier': 3.0,
}
settings['risk'] = {
    'stop_loss_pct'  : 1.5,    # 固定止損底板 %
    'take_profit_pct': 3.0,    # 固定止盈底板 %
}
"""
import pandas as pd

from .base_strategy import BaseStrategy
from indicators.technical import add_rsi, add_macd, add_atr


class MomentumBreakout(BaseStrategy):
    """
    動量突破策略，繼承 BaseStrategy，相容 TITAN 主迴圈。

    僅做多（LONG），不產生 SHORT 訊號。
    空頭市場由 RiskManager 的每日虧損熔斷自動保護。
    """

    def __init__(self, settings: dict):
        cfg      = settings['strategy']['momentum_breakout']
        risk_cfg = settings.get('risk', {})

        self.window_size       = cfg.get('window_size',        20)
        self.rsi_period        = cfg.get('rsi_period',         14)
        self.rsi_overbought    = cfg.get('rsi_overbought',     70)
        self.rsi_oversold      = cfg.get('rsi_oversold',       40)
        self.volume_ratio_min  = cfg.get('volume_ratio_min',   2.0)
        self.require_macd_cross = cfg.get('require_macd_cross', True)

        self.use_atr_sl        = cfg.get('use_atr_sl',         True)
        self.atr_period        = cfg.get('atr_period',         14)
        self.atr_sl_multiplier = cfg.get('atr_sl_multiplier',  1.5)
        self.atr_tp_multiplier = cfg.get('atr_tp_multiplier',  3.0)

        self.stop_loss_pct   = risk_cfg.get('stop_loss_pct',   1.5)
        self.take_profit_pct = risk_cfg.get('take_profit_pct', 3.0)

        self._rsi_col = f'rsi_{self.rsi_period}'
        self._atr_col = f'atr_{self.atr_period}'
        self._last_atr: float = 0.0

    # ------------------------------------------------------------------
    # 私有：準備指標
    # ------------------------------------------------------------------

    def _prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        if self._rsi_col not in df.columns:
            df = add_rsi(df, self.rsi_period)       # Wilder's RSI（精確版）
        if 'macd_hist' not in df.columns:
            df = add_macd(df)
        if self.use_atr_sl and self._atr_col not in df.columns:
            df = add_atr(df, self.atr_period)
        # 成交量均線（window_size 根）
        vol_ma_col = f'vol_ma_{self.window_size}'
        if vol_ma_col not in df.columns:
            df[vol_ma_col] = df['volume'].rolling(self.window_size).mean()
        return df

    # ------------------------------------------------------------------
    # BaseStrategy 實作
    # ------------------------------------------------------------------

    def calculate_signals(self, df: pd.DataFrame) -> str:
        """
        分析最後一根 K 線，回傳 'LONG' 或 'HOLD'。
        此策略不產生 SHORT 訊號。
        """
        min_rows = max(self.window_size, self.rsi_period, 26) + 2
        if df.empty or len(df) < min_rows:
            return 'HOLD'

        df = self._prepare(df)

        last       = df.iloc[-1]
        prev       = df.iloc[-2]
        vol_ma_col = f'vol_ma_{self.window_size}'

        # 基礎數據檢查
        rsi_val = last.get(self._rsi_col)
        if pd.isna(rsi_val):
            return 'HOLD'

        # ── 1. 價格突破：收盤 > 過去 window_size 根的最高點 ──
        lookback_highs = df['high'].iloc[-(self.window_size + 1):-1]
        price_breakthrough = float(last['close']) > float(lookback_highs.max())

        # ── 2. 成交量確認：本根量 > 均量 × ratio ──
        vol_ma = last.get(vol_ma_col)
        if pd.isna(vol_ma) or vol_ma <= 0:
            return 'HOLD'
        volume_ok = float(last['volume']) > float(vol_ma) * self.volume_ratio_min

        # ── 3. RSI 過濾（不追超買，不撿超賣）──
        rsi_ok = self.rsi_oversold < float(rsi_val) < self.rsi_overbought

        # ── 4. MACD 金叉且主線 > 0（動能向上）──
        macd_ok = True
        if self.require_macd_cross:
            macd_now  = last.get('macd')
            hist_now  = last.get('macd_hist')
            hist_prev = prev.get('macd_hist')
            if any(pd.isna(v) for v in [macd_now, hist_now, hist_prev]):
                macd_ok = False
            else:
                # 金叉：本根柱狀圖 > 0 且從負轉正（或持續為正且上升）
                macd_golden_cross = (
                    float(hist_now) > float(hist_prev)
                    and float(hist_now) > 0
                    and float(macd_now) > 0
                )
                macd_ok = macd_golden_cross

        # ── 更新最新 ATR（供止損計算）──
        if self.use_atr_sl and self._atr_col in df.columns:
            atr_val = df[self._atr_col].iloc[-1]
            self._last_atr = float(atr_val) if not pd.isna(atr_val) else 0.0

        # ── 全部通過 → LONG ──
        if price_breakthrough and volume_ok and rsi_ok and macd_ok:
            return 'LONG'
        return 'HOLD'

    @staticmethod
    def _smart_round(price: float) -> float:
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
        ATR 動態止損，1.5% 固定為底板（只放大不縮小）。
        """
        min_sl_dist = entry_price * (self.stop_loss_pct / 100)
        if self.use_atr_sl and self._last_atr > 0:
            sl_dist = max(self._last_atr * self.atr_sl_multiplier, min_sl_dist)
        else:
            sl_dist = min_sl_dist

        if signal == 'LONG':
            return self._smart_round(entry_price - sl_dist)
        return self._smart_round(entry_price)

    def get_take_profit(self, entry_price: float, signal: str) -> float:
        """
        ATR 動態止盈，3.0% 固定為底板（只放大不縮小）。
        """
        min_tp_dist = entry_price * (self.take_profit_pct / 100)
        if self.use_atr_sl and self._last_atr > 0:
            tp_dist = max(self._last_atr * self.atr_tp_multiplier, min_tp_dist)
        else:
            tp_dist = min_tp_dist

        if signal == 'LONG':
            return self._smart_round(entry_price + tp_dist)
        return self._smart_round(entry_price)

    def __repr__(self) -> str:
        return (
            f"MomentumBreakout v1.0("
            f"window={self.window_size}, "
            f"rsi=[{self.rsi_oversold}-{self.rsi_overbought}], "
            f"vol>{self.volume_ratio_min}x, "
            f"SL={self.stop_loss_pct}%, TP={self.take_profit_pct}%)"
        )

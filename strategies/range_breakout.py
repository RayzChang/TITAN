"""
Range Breakout Strategy — 箱體突破策略 v1.0

箱體建立邏輯
------------
1. 在日線 K 線中找「單根最大成交量」的 K 棒作為錨點
2. 箱體下緣 = 錨點 K 棒的最低點
3. 箱體上緣 = 從錨點至今的最高點（假突破會自動延伸）
4. 若收盤跌破下緣且成交量異常 → 箱體失效，以新的大量K棒重建

進場邏輯（多時間週期）
----------------------
- 日線箱體確認突破方向（收盤高於上緣 → 做多；收盤低於下緣 → 做空）
- 1H MACD 交叉確認進場
  · 金叉 → LONG
  · 死叉 → SHORT

止損
----
- 3% of 標的進場價格（非帳戶百分比）
- LONG : entry × 0.97
- SHORT: entry × 1.03

止盈（階梯式，由 main.py 每週期調用 get_management_action）
-----------------------------------------------------------
- TP1 : 4H MACD 反向交叉 → 止盈一半，止損移至開倉價
- TP2+: 持續追蹤，止損階梯跟進

持倉管理
--------
- 日線 MACD 死叉 → 多單減半
- 日線 MACD 金叉 → 空單減半

加倉
----
- 突破確認後，連續 3 根 4H K 棒維持在突破緩衝區（不回頭）→ 加倉
"""

import pandas as pd
from .base_strategy import BaseStrategy
from indicators.technical import add_macd


class RangeBreakout(BaseStrategy):
    """
    箱體突破策略（支援做多 / 做空）

    多時間週期架構：
    ┌─────────┬──────────────────────────────────┐
    │ 日線    │ 箱體偵測、持倉管理（MACD）        │
    │ 4小時   │ TP1 觸發、加倉確認               │
    │ 1小時   │ 進場信號（MACD 交叉）             │
    └─────────┴──────────────────────────────────┘

    main.py 使用方式：
        strategy.update_data(df_4h, df_1d)   # 每週期注入多時間週期資料
        signal = strategy.calculate_signals(df_1h)
        action = strategy.get_management_action(symbol, open_signal)
    """

    def __init__(self, settings: dict):
        cfg = settings.get('strategy', {}).get('range_breakout', {})

        # ── 箱體偵測參數 ──────────────────────────────────────────────
        self.box_lookback      = cfg.get('box_lookback', 90)      # 日線回望根數
        self.anchor_vol_pct    = cfg.get('anchor_vol_pct', 0.0)   # 最大量 K 棒百分位（0=取最大）
        self.breakdown_vol_min = cfg.get('breakdown_vol_min', 1.5) # 跌破失效需要的量比閾值

        # ── 止損 ──────────────────────────────────────────────────────
        self.sl_pct = cfg.get('sl_pct', 3.0)   # 3% of 進場價

        # ── 加倉確認 ──────────────────────────────────────────────────
        self.addon_candles = cfg.get('addon_candles', 3)   # 4H 確認根數

        # ── 多時間週期資料（由 main.py 透過 update_data 注入）────────
        self._df_4h: pd.DataFrame | None = None
        self._df_1d: pd.DataFrame | None = None

        # ── 當前箱體狀態 ──────────────────────────────────────────────
        self._box_upper: float | None = None
        self._box_lower: float | None = None
        self._anchor_idx: int | None  = None   # 錨點在 df_1d 中的位置

        # ── 每 symbol 的持倉狀態 ──────────────────────────────────────
        # key: symbol → {'tp1_done': bool, 'addon_done': bool}
        self._pos_states: dict = {}

    # ==================================================================
    # 資料注入
    # ==================================================================

    def update_data(self, df_4h: pd.DataFrame, df_1d: pd.DataFrame):
        """
        由 main.py 在每次 calculate_signals 前呼叫。
        注入 4H / 日線資料，並重新偵測箱體。
        """
        self._df_4h = df_4h
        self._df_1d = df_1d
        self._detect_box(df_1d)

    # ==================================================================
    # 箱體偵測（核心算法）
    # ==================================================================

    def _detect_box(self, df_1d: pd.DataFrame):
        """
        Volume-Anchored Box Detection

        規則：
        1. 找過去 box_lookback 根日線中「單根最大成交量」的 K 棒（錨點）
        2. 箱體下緣 = 錨點 K 棒的最低點
        3. 箱體上緣 = 從錨點至今所有 K 棒的最高點（假突破自動延伸）
        4. 若最新收盤 < 箱體下緣 → 視為跌破，箱體失效重建（尋找新錨點）
        """
        if df_1d is None or len(df_1d) < 10:
            self._box_upper = self._box_lower = None
            return

        df = df_1d.tail(self.box_lookback).copy().reset_index(drop=True)
        n  = len(df)

        # ── Step 1：找最大量錨點 ──────────────────────────────────────
        anchor_idx = int(df['volume'].idxmax())
        self._anchor_idx = anchor_idx

        # ── Step 2：箱體下緣 = 錨點之後（含錨點）所有 K 棒最低點 ────
        box_lower = float(df['low'].iloc[anchor_idx:].min())

        # ── Step 3：箱體上緣 = 錨點之後（含錨點）所有 K 棒最高點 ─────
        box_upper = float(df['high'].iloc[anchor_idx:].max())

        # ── Step 4：檢查是否已有跌破（箱體失效，從跌破點後重建）──────
        #   若最新收盤已低於下緣，找跌破後的最大量 K 棒作為新錨點
        latest_close = float(df['close'].iloc[-1])
        if latest_close < box_lower:
            # 找跌破點後最大量 K 棒
            breakdown_zone = df.iloc[anchor_idx:]
            below_mask     = breakdown_zone['close'] < box_lower
            if below_mask.any():
                first_below = below_mask.idxmax()                    # 第一根跌破的 index
                post_break  = df.iloc[first_below:].copy()
                if len(post_break) >= 3:
                    new_anchor_idx = int(post_break['volume'].idxmax())
                    box_lower      = float(post_break['low'].iloc[new_anchor_idx - first_below
                                                                  if new_anchor_idx > first_below
                                                                  else 0])
                    box_upper      = float(post_break['high'].iloc[new_anchor_idx - first_below
                                                                   if new_anchor_idx > first_below
                                                                   else 0:].max())
                    self._anchor_idx = new_anchor_idx

        self._box_upper = box_upper
        self._box_lower = box_lower

    # ==================================================================
    # 進場信號（1H MACD）
    # ==================================================================

    def calculate_signals(self, df: pd.DataFrame) -> str:
        """
        計算 1H 進場信號。

        條件：
        1. 箱體已偵測（日線錨點存在）
        2. 日線收盤已突破箱體方向（用最新 1H close 代替）
        3. 1H MACD 交叉確認方向

        Returns
        -------
        'LONG' / 'SHORT' / 'HOLD'
        """
        if self._box_upper is None or self._box_lower is None:
            return 'HOLD'

        if df is None or len(df) < 40:
            return 'HOLD'

        df = df.copy()
        if 'macd_hist' not in df.columns:
            df = add_macd(df)

        last = df.iloc[-1]
        prev = df.iloc[-2]

        hist_now  = last.get('macd_hist')
        hist_prev = prev.get('macd_hist')

        if pd.isna(hist_now) or pd.isna(hist_prev):
            return 'HOLD'

        hist_now  = float(hist_now)
        hist_prev = float(hist_prev)
        close     = float(last['close'])

        # ── 突破上緣 → 等 1H 金叉做多 ────────────────────────────────
        if close > self._box_upper:
            if hist_now > 0 and hist_prev <= 0:   # MACD 柱狀圖由負轉正
                return 'LONG'

        # ── 跌破下緣 → 等 1H 死叉做空 ────────────────────────────────
        elif close < self._box_lower:
            if hist_now < 0 and hist_prev >= 0:   # MACD 柱狀圖由正轉負
                return 'SHORT'

        return 'HOLD'

    # ==================================================================
    # 止損 / 止盈
    # ==================================================================

    def get_stop_loss(self, entry_price: float, signal: str) -> float:
        """
        止損 = 箱體邊界外 1%（Version B：直接用最終止損，不做 3% 中間減倉）
          LONG : 箱體下緣 × 0.99
          SHORT: 箱體上緣 × 1.01
        若箱體尚未偵測到，fallback 為進場價 ± sl_pct%。
        """
        if signal == 'LONG' and self._box_lower:
            return self._round(self._box_lower * 0.99)
        if signal == 'SHORT' and self._box_upper:
            return self._round(self._box_upper * 1.01)
        # fallback
        dist = entry_price * (self.sl_pct / 100)
        if signal == 'LONG':
            return self._round(entry_price - dist)
        return self._round(entry_price + dist)

    def get_take_profit(self, entry_price: float, signal: str) -> float:
        """
        初始掛單 TP = 6%（2:1 風報比保底）
        真正 TP1 由 4H MACD 反向交叉觸發（見 get_management_action）
        """
        dist = entry_price * (self.sl_pct * 2 / 100)
        if signal == 'LONG':
            return self._round(entry_price + dist)
        return self._round(entry_price - dist)

    # ==================================================================
    # 持倉管理（每週期由 main.py 調用）
    # ==================================================================

    def get_management_action(self, symbol: str, signal: str) -> dict:
        """
        回傳對現有持倉應採取的動作。

        Returns
        -------
        dict:
            'tp1'    : bool — 4H MACD 反向交叉，止盈一半 + 止損移至 BE
            'reduce' : bool — 日線 MACD 反向，減半倉
            'addon'  : bool — 4H 連續確認，加倉
        """
        state  = self._pos_states.setdefault(symbol, {'tp1_done': False, 'addon_done': False})
        result = {'tp1': False, 'reduce': False, 'addon': False}

        # ── TP1：4H MACD 反向交叉 ─────────────────────────────────────
        if not state['tp1_done'] and self._has_4h_reversal(signal):
            result['tp1']    = True
            state['tp1_done'] = True

        # ── 日線 MACD 管理 ────────────────────────────────────────────
        if self._has_daily_reversal(signal):
            result['reduce'] = True

        # ── 加倉：4H 連續 N 根確認 ────────────────────────────────────
        if not state['addon_done'] and self._check_addon(signal):
            result['addon']    = True
            state['addon_done'] = True

        return result

    def clear_position_state(self, symbol: str):
        """持倉關閉後清除狀態（由 main.py 在平倉時呼叫）"""
        self._pos_states.pop(symbol, None)

    # ==================================================================
    # 私有輔助
    # ==================================================================

    def _has_4h_reversal(self, signal: str) -> bool:
        """4H MACD 反向交叉（TP1 觸發條件）"""
        df4 = self._df_4h
        if df4 is None or len(df4) < 40:
            return False
        df4 = df4.copy()
        if 'macd_hist' not in df4.columns:
            df4 = add_macd(df4)
        h_now  = float(df4['macd_hist'].iloc[-1])
        h_prev = float(df4['macd_hist'].iloc[-2])
        if signal == 'LONG':
            return h_now < 0 and h_prev >= 0   # 死叉 → 多單 TP1
        if signal == 'SHORT':
            return h_now > 0 and h_prev <= 0   # 金叉 → 空單 TP1
        return False

    def _has_daily_reversal(self, signal: str) -> bool:
        """日線 MACD 反向交叉（持倉減半條件）"""
        df1d = self._df_1d
        if df1d is None or len(df1d) < 40:
            return False
        df1d = df1d.copy()
        if 'macd_hist' not in df1d.columns:
            df1d = add_macd(df1d)
        h_now  = float(df1d['macd_hist'].iloc[-1])
        h_prev = float(df1d['macd_hist'].iloc[-2])
        if signal == 'LONG':
            return h_now < 0 and h_prev >= 0
        if signal == 'SHORT':
            return h_now > 0 and h_prev <= 0
        return False

    def _check_addon(self, signal: str) -> bool:
        """
        加倉條件：突破後連續 N 根 4H K 棒維持在突破方向，不回頭。
        - LONG : 連續 N 根 4H 低點都高於箱體上緣（站穩上方）
        - SHORT: 連續 N 根 4H 高點都低於箱體下緣（站穩下方）
        """
        if self._box_upper is None or self._box_lower is None:
            return False
        df4 = self._df_4h
        if df4 is None or len(df4) < self.addon_candles:
            return False
        recent = df4.tail(self.addon_candles)
        if signal == 'LONG':
            return bool((recent['low'] > self._box_upper).all())
        if signal == 'SHORT':
            return bool((recent['high'] < self._box_lower).all())
        return False

    @staticmethod
    def _round(price: float) -> float:
        if price >= 1000:
            return round(price, 2)
        elif price >= 100:
            return round(price, 3)
        elif price >= 1:
            return round(price, 4)
        else:
            return round(price, 6)

    # ==================================================================
    # 資訊查詢
    # ==================================================================

    def get_box(self) -> tuple:
        """回傳當前箱體（上緣, 下緣）"""
        return self._box_upper, self._box_lower

    def __repr__(self) -> str:
        if self._box_upper and self._box_lower:
            box_str = f"箱體 {self._box_lower:.2f} ~ {self._box_upper:.2f}"
        else:
            box_str = "箱體未偵測"
        return f"RangeBreakout v1.0 | SL={self.sl_pct}% | {box_str}"

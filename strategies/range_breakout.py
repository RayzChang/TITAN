"""
Range Breakout Strategy — 箱體突破策略 v1.1

箱體結構（層疊保留）
--------------------
- floor    : 原始箱體下緣，永久固定，跌破才算失效
- ceilings : 上緣列表（由低到高），每次假突破自動新增一層
- 任一 ceiling 被突破 → 可做多
- floor 被跌破        → 可做空
- floor 跌破 + 高量   → 整組箱體失效，重新偵測

初始箱體
--------
- 優先從 settings.yaml manual_boxes 讀取（朋友人工確認的箱體）
- 若無手動設定，才使用 Volume-Anchored 算法自動偵測

進場邏輯
--------
- 1H MACD 交叉觸發（金叉多 / 死叉空），不需日線確認
- 日線 MACD 僅作為加倉依據

止損（Version B）
-----------------
- 做多：floor × 0.99
- 做空：最高 ceiling × 1.01

止盈
----
- TP1：4H MACD 反向交叉 → 平倉一半，止損移至 BE
- TP2：BE 後 1H MACD 再次反向 → 平倉剩餘

加倉
----
- 4H 連續 3 根確認 + 日線 MACD 方向一致
"""

import pandas as pd
from .base_strategy import BaseStrategy
from indicators.technical import add_macd


class RangeBreakout(BaseStrategy):
    """
    箱體突破策略 v1.1（層疊箱體結構）

    箱體狀態（per symbol）：
    ┌─────────────────────────────────────────────────────┐
    │ floor     : float        — 永久下緣                  │
    │ ceilings  : list[float]  — 上緣列表（低到高）        │
    │ invalidated: bool        — 是否已失效（等待重建）     │
    └─────────────────────────────────────────────────────┘

    main.py 使用方式：
        strategy.init_box(symbol)                    # 初始化箱體
        strategy.update_data(df_4h, df_1d)           # 注入多週期資料
        signal = strategy.calculate_signals(df, symbol)
        action = strategy.get_management_action(symbol, open_signal)
    """

    def __init__(self, settings: dict):
        cfg = settings.get('strategy', {}).get('range_breakout', {})

        # ── 箱體偵測參數 ──────────────────────────────────────────────
        self.box_lookback      = cfg.get('box_lookback', 120)
        self.breakdown_vol_min = cfg.get('breakdown_vol_min', 1.5)
        self.sl_pct            = cfg.get('sl_pct', 3.0)
        self.addon_candles     = cfg.get('addon_candles', 3)

        # 手動初始箱體設定（from settings.yaml）
        self._manual_boxes: dict = cfg.get('manual_boxes', {})

        # ── 多時間週期資料 ─────────────────────────────────────────────
        self._df_4h: pd.DataFrame | None = None
        self._df_1d: pd.DataFrame | None = None

        # ── 每 symbol 的箱體狀態 ──────────────────────────────────────
        # { symbol: {'floor': float, 'ceilings': [float,...], 'invalidated': bool} }
        self._boxes: dict = {}

        # ── 每 symbol 的持倉狀態 ──────────────────────────────────────
        self._pos_states: dict = {}

    # ==================================================================
    # 箱體初始化（由 main.py 在啟動時對每個 symbol 呼叫一次）
    # ==================================================================

    def init_box(self, symbol: str, df_1d: pd.DataFrame | None = None):
        """
        初始化指定 symbol 的箱體。
        優先使用 manual_boxes，否則用自動偵測。
        """
        # 統一 symbol 格式（BTC/USDT:USDT → BTC/USDT）
        key = symbol.split(':')[0]

        if key in self._manual_boxes:
            mb = self._manual_boxes[key]
            floor    = float(mb['floor'])
            ceilings = sorted([float(c) for c in mb['ceilings']])
            self._boxes[symbol] = {
                'floor':       floor,
                'ceilings':    ceilings,
                'invalidated': False,
            }
            from utils.logger import get_logger
            get_logger().info(
                f"[箱體] {symbol} 手動初始化 | "
                f"floor={floor} | ceilings={ceilings}"
            )
        elif df_1d is not None:
            self._rebuild_box_from_data(symbol, df_1d)
        else:
            self._boxes[symbol] = {
                'floor': None, 'ceilings': [], 'invalidated': True
            }

    # ==================================================================
    # 資料注入
    # ==================================================================

    def update_data(self, df_4h: pd.DataFrame, df_1d: pd.DataFrame):
        """每週期由 main.py 注入最新多時間週期資料"""
        self._df_4h = df_4h
        self._df_1d = df_1d

    # ==================================================================
    # 箱體動態更新（每週期掃描時呼叫）
    # ==================================================================

    def update_box(self, symbol: str, current_high: float,
                   current_close: float, current_volume: float):
        """
        根據最新 K 棒資料更新箱體狀態：
        1. 若 high > 最高 ceiling → 新增延伸上緣
        2. 若 close < floor AND 高量 → 箱體失效，重建
        """
        box = self._boxes.get(symbol)
        if box is None or box['invalidated']:
            return

        floor    = box['floor']
        ceilings = box['ceilings']

        # ── 上緣延伸（假突破或真突破都先延伸記錄）──
        top = ceilings[-1] if ceilings else 0
        if current_high > top * 1.001:   # 至少突破 0.1% 才算新高
            box['ceilings'].append(round(current_high, 2))

        # ── 箱體失效偵測 ──
        if floor is not None and current_close < floor:
            avg_vol = self._get_avg_volume(symbol)
            if avg_vol and current_volume >= avg_vol * self.breakdown_vol_min:
                box['invalidated'] = True
                from utils.logger import get_logger
                get_logger().warning(
                    f"[箱體] {symbol} 失效！close={current_close} < floor={floor} "
                    f"vol={current_volume:.0f} >= avg×{self.breakdown_vol_min}"
                )
                # 嘗試用日線資料重建
                if self._df_1d is not None:
                    self._rebuild_box_from_data(symbol, self._df_1d)

    def _get_avg_volume(self, symbol: str) -> float | None:
        if self._df_1d is None or len(self._df_1d) < 10:
            return None
        return float(self._df_1d['volume'].mean())

    def _rebuild_box_from_data(self, symbol: str, df_1d: pd.DataFrame):
        """箱體失效後，用 Volume-Anchored 算法重建新箱體"""
        df = df_1d.tail(self.box_lookback).copy().reset_index(drop=True)
        if len(df) < 10:
            return

        avg_vol    = df['volume'].mean()
        anchor_idx = int(df['volume'].idxmax())
        form_end   = min(anchor_idx + 5, len(df) - 1)

        floor    = float(df['low'].iloc[anchor_idx:form_end + 1].min())
        ceiling  = float(df['high'].iloc[anchor_idx:form_end + 1].max())
        # running max from anchor
        final_ceil = float(df['high'].iloc[anchor_idx:].max())

        ceilings = sorted(set([ceiling, final_ceil]))

        self._boxes[symbol] = {
            'floor':       floor,
            'ceilings':    ceilings,
            'invalidated': False,
        }
        from utils.logger import get_logger
        get_logger().info(
            f"[箱體] {symbol} 自動重建 | floor={floor} | ceilings={ceilings}"
        )

    # ==================================================================
    # 進場信號（1H MACD）
    # ==================================================================

    def calculate_signals(self, df: pd.DataFrame, symbol: str = '') -> str:
        """
        計算 1H 進場信號。

        做多條件：
          - 現價 > 任一 ceiling（突破任一上緣）
          - 1H MACD 金叉

        做空條件：
          - 現價 < floor（跌破永久下緣）
          - 1H MACD 死叉

        Returns: 'LONG' / 'SHORT' / 'HOLD'
        """
        box = self._boxes.get(symbol, {})
        floor    = box.get('floor')
        ceilings = box.get('ceilings', [])

        if floor is None or not ceilings:
            return 'HOLD'

        if df is None or len(df) < 40:
            return 'HOLD'

        df = df.copy()
        if 'macd_hist' not in df.columns:
            df = add_macd(df)

        last = df.iloc[-1]
        prev = df.iloc[-2]

        hist_now  = float(last.get('macd_hist', 0) or 0)
        hist_prev = float(prev.get('macd_hist', 0) or 0)
        close     = float(last['close'])

        if pd.isna(hist_now) or pd.isna(hist_prev):
            return 'HOLD'

        gold_cross = hist_now > 0 and hist_prev <= 0
        dead_cross = hist_now < 0 and hist_prev >= 0

        # ── 做多：突破任一上緣 + 金叉 ────────────────────────────────
        top_ceiling = ceilings[-1]
        if close > top_ceiling and gold_cross:
            return 'LONG'

        # 次級上緣：突破任一中間層也可進場
        for ceil in ceilings[:-1]:
            if close > ceil and gold_cross:
                return 'LONG'

        # ── 做空：跌破 floor + 死叉 ──────────────────────────────────
        if close < floor and dead_cross:
            return 'SHORT'

        return 'HOLD'

    # ==================================================================
    # 止損 / 止盈
    # ==================================================================

    def get_stop_loss(self, entry_price: float, signal: str,
                      symbol: str = '') -> float:
        """
        Version B 止損：
          LONG : floor × 0.99
          SHORT: 最高 ceiling × 1.01
        """
        box      = self._boxes.get(symbol, {})
        floor    = box.get('floor')
        ceilings = box.get('ceilings', [])

        if signal == 'LONG' and floor:
            return self._round(floor * 0.99)
        if signal == 'SHORT' and ceilings:
            return self._round(ceilings[-1] * 1.01)

        # fallback
        dist = entry_price * (self.sl_pct / 100)
        return self._round(entry_price - dist if signal == 'LONG'
                           else entry_price + dist)

    def get_take_profit(self, entry_price: float, signal: str,
                        symbol: str = '') -> float:
        """初始掛單 TP = 6%（TP1 由 4H MACD 實際觸發覆蓋）"""
        dist = entry_price * (self.sl_pct * 2 / 100)
        if signal == 'LONG':
            return self._round(entry_price + dist)
        return self._round(entry_price - dist)

    # ==================================================================
    # 持倉管理
    # ==================================================================

    def get_management_action(self, symbol: str, signal: str) -> dict:
        """
        回傳對現有持倉應採取的動作。
          tp1    : 4H MACD 反向交叉 → 止盈一半 + 止損移至 BE
          reduce : 日線 MACD 反向   → 減半倉
          addon  : 4H 連續確認      → 加倉
        """
        state  = self._pos_states.setdefault(
            symbol, {'tp1_done': False, 'addon_done': False}
        )
        result = {'tp1': False, 'reduce': False, 'addon': False}

        if not state['tp1_done'] and self._has_4h_reversal(signal):
            result['tp1']     = True
            state['tp1_done'] = True

        if self._has_daily_reversal(signal):
            result['reduce'] = True

        if not state['addon_done'] and self._check_addon(signal, symbol):
            result['addon']      = True
            state['addon_done']  = True

        return result

    def clear_position_state(self, symbol: str):
        self._pos_states.pop(symbol, None)

    # ==================================================================
    # 查詢介面
    # ==================================================================

    def get_box(self, symbol: str = '') -> tuple:
        """
        回傳 (floor, top_ceiling) 供 main.py 顯示用。
        舊版相容：若無 symbol 則回傳第一個箱體。
        """
        if symbol and symbol in self._boxes:
            box = self._boxes[symbol]
            top = box['ceilings'][-1] if box['ceilings'] else None
            return top, box['floor']
        # 舊版相容（單箱體模式）
        if self._boxes:
            box = next(iter(self._boxes.values()))
            top = box['ceilings'][-1] if box['ceilings'] else None
            return top, box['floor']
        return None, None

    def get_box_detail(self, symbol: str) -> dict:
        """回傳完整箱體資訊"""
        return self._boxes.get(symbol, {})

    # ==================================================================
    # 私有輔助
    # ==================================================================

    def _has_4h_reversal(self, signal: str) -> bool:
        df4 = self._df_4h
        if df4 is None or len(df4) < 40:
            return False
        df4 = df4.copy()
        if 'macd_hist' not in df4.columns:
            df4 = add_macd(df4)
        h_now  = float(df4['macd_hist'].iloc[-1])
        h_prev = float(df4['macd_hist'].iloc[-2])
        if signal == 'LONG':
            return h_now < 0 and h_prev >= 0
        if signal == 'SHORT':
            return h_now > 0 and h_prev <= 0
        return False

    def _has_daily_reversal(self, signal: str) -> bool:
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

    def _check_addon(self, signal: str, symbol: str = '') -> bool:
        box      = self._boxes.get(symbol, {})
        floor    = box.get('floor')
        ceilings = box.get('ceilings', [])
        if not floor or not ceilings:
            return False
        df4 = self._df_4h
        if df4 is None or len(df4) < self.addon_candles:
            return False
        recent = df4.tail(self.addon_candles)
        top = ceilings[-1]
        if signal == 'LONG':
            return bool((recent['low'] > top).all())
        if signal == 'SHORT':
            return bool((recent['high'] < floor).all())
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

    def __repr__(self) -> str:
        parts = []
        for sym, box in self._boxes.items():
            top = box['ceilings'][-1] if box['ceilings'] else '?'
            parts.append(f"{sym.split('/')[0]}: {box['floor']}~{top}")
        box_str = ' | '.join(parts) if parts else '箱體未初始化'
        return f"RangeBreakout v1.1 | {box_str}"

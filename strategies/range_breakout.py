"""
Range Breakout Strategy — 箱體突破策略 v1.2
TITAN v1.3 規格書實作

箱體結構（層疊保留）
--------------------
- floor     : 原始箱體下緣，永久固定（形成期最低價）
- ceilings  : 上緣列表（由低到高），假突破確認後才新增一層
- 任一 ceiling 被突破 + 1H 金叉 → 做多
- floor 被跌破 + 1H 死叉         → 做空
- floor 跌破 + 最近20日高量      → 整組箱體失效

進場規則（防重複追單）
-----------------------
- 進場後記錄 base_ceiling（本次基準上緣）
- 必須先回到 base_ceiling 下方，才允許重新進場

止損（Version B）
-----------------
- 做多：floor × 0.99（永久固定）
- 做空：進場當下最高 ceiling × 1.01（凍結，不跟著延伸）

止盈
----
- TP1：4H MACD 反向交叉 → 平一半，止損移至 BE
- TP2：TP1 後 1H MACD 再次反向 → 全平
- TP1 觸發時若 1H 同時反向 → 直接全平，不留半倉

加倉
----
- TP1 之前才可加倉（加一次）
- 4H 連續 3 根最低 > 基準上緣
- 最新已收日線 MACD hist > 0（多）/ < 0（空）
"""

import pandas as pd
from .base_strategy import BaseStrategy
from indicators.technical import add_macd


class RangeBreakout(BaseStrategy):
    """箱體突破策略 v1.2（TITAN v1.3 規格）"""

    def __init__(self, settings: dict):
        cfg = self._get_cfg(settings)

        self.box_lookback      = cfg.get('box_lookback', 120)
        self.breakdown_vol_min = cfg.get('breakdown_vol_min', 1.5)
        self.sl_pct            = cfg.get('sl_pct', 3.0)
        self.addon_candles     = cfg.get('addon_candles', 3)
        self._manual_boxes: dict = cfg.get('manual_boxes', {})

        # 多時間週期資料
        self._df_4h: pd.DataFrame | None = None
        self._df_1d: pd.DataFrame | None = None

        # per-symbol 箱體狀態
        # { symbol: {
        #     floor, ceilings, invalidated,
        #     tracking_breakout, breakout_max_high, breakout_ref_ceiling
        # } }
        self._boxes: dict = {}

        # per-symbol 持倉管理狀態（持倉期間：tp1_done, addon_done, frozen_sl）
        self._pos_states: dict = {}

        # per-symbol 防重複追單狀態（獨立於 pos_state，平倉後仍保留）
        # { symbol: {
        #     'needs_reset': bool,   ← 是否需要先回到 ref_price 才允許再進場
        #     'signal_dir': str,     ← 'LONG' or 'SHORT'
        #     'ref_price': float,    ← LONG=base_ceiling, SHORT=floor
        # } }
        self._anti_repeat: dict = {}

    @staticmethod
    def _get_cfg(settings: dict) -> dict:
        return settings.get('strategy', {}).get('range_breakout', {})

    # ==================================================================
    # 箱體初始化
    # ==================================================================

    def init_box(self, symbol: str, df_1d: pd.DataFrame | None = None):
        """初始化指定 symbol 的箱體（手動種子 or 自動偵測）"""
        key = symbol.split(':')[0]

        if key in self._manual_boxes:
            mb = self._manual_boxes[key]
            floor    = float(mb['floor'])
            ceilings = sorted([float(c) for c in mb['ceilings']])
            self._boxes[symbol] = {
                'floor':               floor,
                'ceilings':            ceilings,
                'invalidated':         False,
                'source':              'manual_seed',
                'tracking_breakout':   False,
                'breakout_max_high':   None,
                'breakout_ref_ceiling': None,
            }
            from utils.logger import get_logger
            get_logger().info(
                f"[箱體] {symbol} 手動初始化 | floor={floor} | ceilings={ceilings}"
            )
        elif df_1d is not None:
            self._rebuild_box_from_data(symbol, df_1d)
        else:
            self._boxes[symbol] = self._empty_box()

    def _empty_box(self) -> dict:
        return {
            'floor': None, 'ceilings': [], 'invalidated': True,
            'source': None,
            'tracking_breakout': False, 'breakout_max_high': None,
            'breakout_ref_ceiling': None,
        }

    # ==================================================================
    # 資料注入
    # ==================================================================

    def update_data(self, df_4h: pd.DataFrame, df_1d: pd.DataFrame):
        self._df_4h = df_4h
        self._df_1d = df_1d

    # ==================================================================
    # 箱體動態更新（R5: 假突破確認後才加層）
    # ==================================================================

    def update_box(self, symbol: str, current_high: float,
                   current_close: float, current_volume: float):
        """
        根據最新已收 K 棒資料更新箱體：
        1. 上緣延伸：只在「確認假突破後」新增一層（R5）
        2. 箱體失效：close < floor + 最近20日高量（R6）
        """
        box = self._boxes.get(symbol)
        if box is None or box['invalidated']:
            return

        floor    = box['floor']
        ceilings = box['ceilings']
        if not ceilings:
            return

        top = ceilings[-1]

        # ── 上緣延伸狀態機（R5）──
        if not box['tracking_breakout']:
            # 尚未突破：若此棒收盤 > 最高上緣，進入追蹤
            if current_close > top:
                box['tracking_breakout']   = True
                box['breakout_max_high']   = current_high
                box['breakout_ref_ceiling'] = top
        else:
            # 正在追蹤假突破
            if current_high > (box['breakout_max_high'] or 0):
                box['breakout_max_high'] = current_high
            if current_close < box['breakout_ref_ceiling']:
                # 收盤跌回舊上緣 → 確認假突破，加入新層
                new_ceil = round(box['breakout_max_high'], 2)
                if new_ceil > top:
                    box['ceilings'].append(new_ceil)
                    from utils.logger import get_logger
                    get_logger().info(
                        f"[箱體] {symbol} 假突破確認 → 新上緣 {new_ceil} | "
                        f"ceilings={box['ceilings']}"
                    )
                box['tracking_breakout']   = False
                box['breakout_max_high']   = None
                box['breakout_ref_ceiling'] = None

        # ── 箱體失效偵測 (R6: 20日均量) ──
        if floor is not None and current_close < floor:
            avg_vol = self._get_avg_volume_20d()
            if avg_vol and current_volume >= avg_vol * self.breakdown_vol_min:
                box['invalidated'] = True
                from utils.logger import get_logger
                get_logger().warning(
                    f"[箱體] {symbol} 失效！close={current_close:.2f} < floor={floor} "
                    f"| vol={current_volume:.0f} >= 20日均量×{self.breakdown_vol_min:.1f}"
                )
                # 只有無持倉時才立刻重建（R2 由 main.py 在平倉後呼叫 rebuild）

    def _get_avg_volume_20d(self) -> float | None:
        """最近 20 根已收日線的平均成交量（R6）"""
        if self._df_1d is None or len(self._df_1d) < 5:
            return None
        # 用 iloc[:-1] 排除今日未收完日線
        closed = self._df_1d.iloc[:-1]
        return float(closed['volume'].tail(20).mean())

    def rebuild_box_if_invalidated(self, symbol: str) -> bool:
        """
        R2：若箱體失效，重建新箱體。
        由 main.py 在平倉後呼叫，確保持倉中不切換箱體。
        回傳是否執行了重建。
        """
        box = self._boxes.get(symbol)
        if box and box['invalidated'] and self._df_1d is not None:
            self._rebuild_box_from_data(symbol, self._df_1d)
            return True
        return False

    def _rebuild_box_from_data(self, symbol: str, df_1d: pd.DataFrame):
        """Volume-Anchored 自動建箱（R8: 錨點後推保護）"""
        df = df_1d.iloc[:-1].tail(self.box_lookback).copy().reset_index(drop=True)
        if len(df) < 10:
            self._boxes[symbol] = self._empty_box()
            return

        # R8：錨點候選必須後面至少還有 4 根已收日線
        vol = df['volume'].copy()
        anchor_idx = None
        for _ in range(len(df)):
            candidate = int(vol.idxmax())
            if candidate + 4 < len(df):
                anchor_idx = candidate
                break
            vol.iloc[candidate] = 0  # 移除這個候選，找次大

        if anchor_idx is None:
            self._boxes[symbol] = self._empty_box()
            return

        form_end = min(anchor_idx + 5, len(df) - 1)
        floor    = float(df['low'].iloc[anchor_idx:form_end + 1].min())
        ceiling  = float(df['high'].iloc[anchor_idx:form_end + 1].max())

        self._boxes[symbol] = {
            'floor':               floor,
            'ceilings':            [ceiling],
            'invalidated':         False,
            'source':              'auto_rebuild',
            'tracking_breakout':   False,
            'breakout_max_high':   None,
            'breakout_ref_ceiling': None,
        }
        from utils.logger import get_logger
        get_logger().info(
            f"[箱體] {symbol} 自動重建 | floor={floor:.2f} | ceilings=[{ceiling:.2f}]"
        )

    # ==================================================================
    # 進場訊號（1H MACD 已收線）
    # ==================================================================

    def calculate_signals(self, df: pd.DataFrame, symbol: str = '') -> str:
        """
        計算 1H 進場訊號（只用已收完 K 棒 → iloc[-2], iloc[-3]）

        Returns: 'LONG' / 'SHORT' / 'HOLD'
        """
        box = self._boxes.get(symbol, {})
        floor    = box.get('floor')
        ceilings = box.get('ceilings', [])

        if floor is None or not ceilings:
            return 'HOLD'
        if box.get('invalidated'):
            return 'HOLD'
        if df is None or len(df) < 40:
            return 'HOLD'

        df = df.copy()
        if 'macd_hist' not in df.columns:
            df = add_macd(df)

        # 只用已收完 K 棒（iloc[-2]=最後已收, iloc[-3]=前一根）
        last = df.iloc[-2]
        prev = df.iloc[-3]

        hist_now  = float(last.get('macd_hist', 0) or 0)
        hist_prev = float(prev.get('macd_hist', 0) or 0)
        close     = float(last['close'])

        if pd.isna(hist_now) or pd.isna(hist_prev):
            return 'HOLD'

        gold_cross = hist_now > 0 and hist_prev <= 0
        dead_cross = hist_now < 0 and hist_prev >= 0

        # ── 防重複追單（R1）── 獨立於 pos_state，平倉後仍生效
        ar = self._anti_repeat.get(symbol)
        if ar and ar.get('needs_reset'):
            ref   = ar.get('ref_price')
            sdir  = ar.get('signal_dir', 'LONG')
            reset = False
            if sdir == 'LONG'  and ref and close < ref:
                reset = True
            elif sdir == 'SHORT' and ref and close > ref:
                reset = True
            if reset:
                ar['needs_reset'] = False
            else:
                return 'HOLD'

        # ── 做多：突破任一上緣 + 金叉 ──
        if gold_cross:
            triggered_ceil = None
            for ceil in sorted(ceilings, reverse=True):
                if close > ceil:
                    triggered_ceil = ceil
                    break
            if triggered_ceil is not None:
                return 'LONG'

        # ── 做空：跌破 floor + 死叉 ──
        if close < floor and dead_cross:
            return 'SHORT'

        return 'HOLD'

    def get_triggered_ceiling(self, df: pd.DataFrame, symbol: str) -> float | None:
        """回傳本次進場觸發的基準上緣（供 main.py 記錄 base_ceiling）"""
        box = self._boxes.get(symbol, {})
        ceilings = box.get('ceilings', [])
        if df is None or len(df) < 2 or not ceilings:
            return None
        df = df.copy()
        if 'macd_hist' not in df.columns:
            df = add_macd(df)
        close = float(df.iloc[-2]['close'])
        for ceil in sorted(ceilings, reverse=True):
            if close > ceil:
                return ceil
        return None

    # ==================================================================
    # 止損 / 止盈
    # ==================================================================

    def get_stop_loss(self, entry_price: float, signal: str,
                      symbol: str = '') -> float:
        """
        Version B 止損：
          LONG : floor × 0.99（永久固定）
          SHORT: 進場當下最高 ceiling × 1.01（由 main.py 凍結後傳入）
        """
        box      = self._boxes.get(symbol, {})
        floor    = box.get('floor')
        ceilings = box.get('ceilings', [])

        if signal == 'LONG' and floor:
            return self._round(floor * 0.99)
        if signal == 'SHORT' and ceilings:
            return self._round(ceilings[-1] * 1.01)

        dist = entry_price * (self.sl_pct / 100)
        return self._round(entry_price - dist if signal == 'LONG'
                           else entry_price + dist)

    def get_frozen_short_sl(self, symbol: str) -> float | None:
        """R3：取得做空時凍結的止損價（進場當下記錄的，不隨延伸變動）"""
        return self._pos_states.get(symbol, {}).get('frozen_sl_ceiling')

    def get_take_profit(self, entry_price: float, signal: str,
                        symbol: str = '') -> float:
        """初始掛單 TP = sl_pct × 2（TP1 由 4H MACD 觸發覆蓋）"""
        dist = entry_price * (self.sl_pct * 2 / 100)
        if signal == 'LONG':
            return self._round(entry_price + dist)
        return self._round(entry_price - dist)

    # ==================================================================
    # 持倉管理狀態（進場 / 平倉 / 查詢）
    # ==================================================================

    def on_position_opened(self, symbol: str, signal: str,
                           base_ceiling: float | None = None):
        """
        進場後登記持倉狀態 + 防重複追單狀態。
        由 main.py 在開倉成功後呼叫。
        """
        box   = self._boxes.get(symbol, {})
        floor = box.get('floor')
        ceilings = box.get('ceilings', [])

        # 凍結做空止損（R3）
        frozen_ceil = ceilings[-1] if (signal == 'SHORT' and ceilings) else None

        self._pos_states[symbol] = {
            'tp1_done':          False,
            'addon_done':        False,
            'frozen_sl_ceiling': frozen_ceil,
        }

        # 防重複追單：LONG → 回到 base_ceiling 下方，SHORT → 回到 floor 上方
        ref_price = base_ceiling if signal == 'LONG' else floor
        self._anti_repeat[symbol] = {
            'needs_reset': True,
            'signal_dir':  signal,
            'ref_price':   ref_price,
        }

    def on_position_closed(self, symbol: str):
        """平倉後清除持倉狀態（anti_repeat 保留，直到回箱體才清）"""
        self._pos_states.pop(symbol, None)
        # 注意：不清 _anti_repeat，讓它繼續擋重複進場

    def get_pos_state(self, symbol: str) -> dict:
        return dict(self._pos_states.get(symbol, {}))

    def restore_pos_state(self, symbol: str, state: dict):
        """啟動對帳時恢復 pos_state（從 state.json）"""
        self._pos_states[symbol] = state

    def get_anti_repeat_state(self, symbol: str) -> dict:
        """供 main.py 讀取並寫入 state.json（Fix 4）"""
        return dict(self._anti_repeat.get(symbol, {}))

    def restore_anti_repeat(self, symbol: str, state: dict):
        """啟動對帳時恢復 anti_repeat（從 state.json）"""
        self._anti_repeat[symbol] = state

    # ==================================================================
    # 持倉管理動作（TP1 / TP2 / addon）
    # ==================================================================

    def get_management_action(self, symbol: str, signal: str) -> dict:
        """
        回傳對現有持倉應採取的動作。
        只用已收完 K 棒。

        Returns dict with keys:
          tp1    : bool — 4H 反向 → 平倉一半 + SL 移至 BE
          tp2    : bool — TP1 後 1H 反向 → 全平（第二段出場）
          tp1_and_tp2: bool — TP1 觸發時 1H 也反向 → 直接全平（R4）
          addon  : bool — 加倉條件成立
        """
        state  = self._pos_states.setdefault(
            symbol, {
                'tp1_done': False, 'addon_done': False,
                'base_ceiling': None, 'needs_reentry_reset': False,
                'frozen_sl_ceiling': None,
            }
        )
        result = {'tp1': False, 'tp2': False, 'tp1_and_tp2': False, 'addon': False}

        if not state['tp1_done']:
            if self._has_4h_reversal(signal):
                # R4：TP1 觸發時若 1H 也反向 → 直接全平
                if self._has_1h_reversal(signal):
                    result['tp1_and_tp2'] = True
                else:
                    result['tp1'] = True
                state['tp1_done'] = True
        else:
            # 第二段出場：1H 反向
            if self._has_1h_reversal(signal):
                result['tp2'] = True

        # addon：TP1 之前才可加倉（R11）
        if not state['tp1_done'] and not state['addon_done']:
            if self._check_addon(signal, symbol):
                result['addon']     = True
                state['addon_done'] = True

        return result

    def clear_position_state(self, symbol: str):
        """alias，相容舊呼叫"""
        self.on_position_closed(symbol)

    # ==================================================================
    # 查詢介面
    # ==================================================================

    def get_box(self, symbol: str = '') -> tuple:
        if symbol and symbol in self._boxes:
            box = self._boxes[symbol]
            top = box['ceilings'][-1] if box['ceilings'] else None
            return top, box['floor']
        if self._boxes:
            box = next(iter(self._boxes.values()))
            top = box['ceilings'][-1] if box['ceilings'] else None
            return top, box['floor']
        return None, None

    def get_box_detail(self, symbol: str) -> dict:
        return self._boxes.get(symbol, {})

    def is_box_invalidated(self, symbol: str) -> bool:
        return self._boxes.get(symbol, {}).get('invalidated', False)

    # ==================================================================
    # 私有輔助
    # ==================================================================

    def _has_4h_reversal(self, signal: str) -> bool:
        """4H MACD 反向交叉（只用已收線 iloc[-2]/-3）"""
        df4 = self._df_4h
        if df4 is None or len(df4) < 40:
            return False
        df4 = df4.copy()
        if 'macd_hist' not in df4.columns:
            df4 = add_macd(df4)
        h_now  = float(df4['macd_hist'].iloc[-2])
        h_prev = float(df4['macd_hist'].iloc[-3])
        if pd.isna(h_now) or pd.isna(h_prev):
            return False
        if signal == 'LONG':
            return h_now < 0 and h_prev >= 0
        if signal == 'SHORT':
            return h_now > 0 and h_prev <= 0
        return False

    def _has_1h_reversal(self, signal: str) -> bool:
        """1H MACD 反向交叉（只用已收線 iloc[-2]/-3）"""
        # 1H 資料由 df 參數注入，這裡需要暫存
        # 由 update_data 注入的 _df_4h 是 4H；1H 要由 main.py 另外傳
        # 暫時 False，等 update_data 同時注入 df_1h 後再啟用
        df1h = getattr(self, '_df_1h', None)
        if df1h is None or len(df1h) < 40:
            return False
        df1h = df1h.copy()
        if 'macd_hist' not in df1h.columns:
            df1h = add_macd(df1h)
        h_now  = float(df1h['macd_hist'].iloc[-2])
        h_prev = float(df1h['macd_hist'].iloc[-3])
        if pd.isna(h_now) or pd.isna(h_prev):
            return False
        if signal == 'LONG':
            return h_now < 0 and h_prev >= 0
        if signal == 'SHORT':
            return h_now > 0 and h_prev <= 0
        return False

    def _has_daily_reversal(self, signal: str) -> bool:
        """日線 MACD 反向（只用已收線 iloc[-2]/-3）"""
        df1d = self._df_1d
        if df1d is None or len(df1d) < 40:
            return False
        df1d = df1d.copy()
        if 'macd_hist' not in df1d.columns:
            df1d = add_macd(df1d)
        h_now  = float(df1d['macd_hist'].iloc[-2])
        h_prev = float(df1d['macd_hist'].iloc[-3])
        if pd.isna(h_now) or pd.isna(h_prev):
            return False
        if signal == 'LONG':
            return h_now < 0 and h_prev >= 0
        if signal == 'SHORT':
            return h_now > 0 and h_prev <= 0
        return False

    def _check_addon(self, signal: str, symbol: str = '') -> bool:
        """
        加倉條件（R7: 加上日線 MACD 持續狀態）：
        - 連續 addon_candles 根已收 4H K 棒確認方向
        - 最新已收日線 MACD hist > 0（多）/ < 0（空）
        """
        box      = self._boxes.get(symbol, {})
        floor    = box.get('floor')
        ceilings = box.get('ceilings', [])
        if not floor or not ceilings:
            return False

        df4 = self._df_4h
        if df4 is None or len(df4) < self.addon_candles + 1:
            return False

        # 只取已收棒（排除最後一根正在形成的）
        closed_4h = df4.iloc[:-1].tail(self.addon_candles)
        top = ceilings[-1]

        if signal == 'LONG':
            price_ok = bool((closed_4h['low'] > top).all())
        elif signal == 'SHORT':
            price_ok = bool((closed_4h['high'] < floor).all())
        else:
            return False

        if not price_ok:
            return False

        # R7：日線 MACD 持續狀態
        df1d = self._df_1d
        if df1d is None or len(df1d) < 30:
            return False
        df1d = df1d.copy()
        if 'macd_hist' not in df1d.columns:
            df1d = add_macd(df1d)
        daily_hist = float(df1d['macd_hist'].iloc[-2])  # 最新已收日線
        if pd.isna(daily_hist):
            return False
        if signal == 'LONG':
            return daily_hist > 0
        if signal == 'SHORT':
            return daily_hist < 0
        return False

    def update_1h_data(self, df_1h: pd.DataFrame):
        """供 main.py 注入 1H 資料，供 _has_1h_reversal 使用"""
        self._df_1h = df_1h

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
        return f"RangeBreakout v1.2 | {box_str}"

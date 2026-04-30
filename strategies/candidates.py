"""
TITAN — MIA 提案的兩條候選策略

A. TrendPullback : 多時框趨勢回調
   - 4H 確認趨勢方向
   - 1H 等回調到 EMA20 附近
   - RSI 過濾，避免抓刀
   - 高頻次（每幾天一次）

B. VolumeMomentum : 成交量爆量動量
   - 1H 成交量超過近 20 根均量 2.5x
   - 同時方向動量明確（K 棒幅度 > 0.5%）
   - 順勢進場，超緊 SL

兩條都用「已收 K 棒」（iloc[-2]）判定，無 lookahead bias。
"""

import pandas as pd
import numpy as np
from .base_strategy import BaseStrategy


def _ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def _rsi(s: pd.Series, length: int = 14) -> pd.Series:
    delta = s.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.rolling(length).mean()
    avg_loss = loss.rolling(length).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _adx(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """ADX 趨勢強度指標"""
    high, low, close = df['high'], df['low'], df['close']
    plus_dm  = (high.diff()).clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)
    plus_dm[plus_dm < minus_dm] = 0
    minus_dm[minus_dm < plus_dm] = 0
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low  - close.shift()).abs()
    tr  = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(length).mean()
    plus_di  = 100 * plus_dm.rolling(length).mean()  / atr.replace(0, np.nan)
    minus_di = 100 * minus_dm.rolling(length).mean() / atr.replace(0, np.nan)
    dx  = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.rolling(length).mean()


# ────────────────────────────────────────────────────────────────────
# 策略 A：TrendPullback — 趨勢回調
# ────────────────────────────────────────────────────────────────────

class TrendPullback(BaseStrategy):
    """
    多時框趨勢回調

    進場邏輯：
      1. 4H：EMA20 vs EMA50 判斷大方向
      2. 1H：等回調到 EMA20 附近（多頭時 low 觸碰 EMA20×1.005 內）
      3. 1H：收盤價回到 EMA20 上方（多頭）
      4. 1H：RSI 從 < 40 翻揚（多頭）or 從 > 60 翻落（空頭）

    SL: 回調的低點 × 0.99（多）/ 高點 × 1.01（空）
    TP: 1:2 R-multiple
    """

    def __init__(self, settings: dict | None = None):
        cfg = (settings or {}).get('strategy', {}).get('trend_pullback', {})
        self.ema_short = cfg.get('ema_short', 20)
        self.ema_long  = cfg.get('ema_long',  50)
        self.rsi_oversold   = cfg.get('rsi_oversold',   40)
        self.rsi_overbought = cfg.get('rsi_overbought', 60)
        self.tp_r_multiple  = cfg.get('tp_r_multiple', 2.0)
        self.sl_buffer      = cfg.get('sl_buffer', 0.005)  # SL 加 0.5% 緩衝
        self._last_sl_distance = 0.0   # 最近一筆 SL 距離（給 TP 用）
        self._df_4h = None

    def update_data(self, df_4h: pd.DataFrame, df_1d=None):
        self._df_4h = df_4h

    def calculate_signals(self, df: pd.DataFrame, symbol: str = '') -> str:
        if df is None or len(df) < max(self.ema_long, 20) + 5:
            return 'HOLD'
        if self._df_4h is None or len(self._df_4h) < self.ema_long + 5:
            return 'HOLD'

        # 4H 趨勢
        e20_4h = _ema(self._df_4h['close'], self.ema_short)
        e50_4h = _ema(self._df_4h['close'], self.ema_long)
        # 用 iloc[-2]（最後已收）
        if pd.isna(e20_4h.iloc[-2]) or pd.isna(e50_4h.iloc[-2]):
            return 'HOLD'
        trend = 'UP' if e20_4h.iloc[-2] > e50_4h.iloc[-2] else 'DOWN'

        # 1H 指標（用已收）
        e20 = _ema(df['close'], self.ema_short)
        rsi = _rsi(df['close'], 14)
        last  = df.iloc[-2]
        prev  = df.iloc[-3]
        if pd.isna(rsi.iloc[-2]) or pd.isna(rsi.iloc[-3]) or pd.isna(e20.iloc[-2]):
            return 'HOLD'

        ema_now = e20.iloc[-2]
        close   = float(last['close'])
        low     = float(last['low'])
        high    = float(last['high'])
        rsi_now  = float(rsi.iloc[-2])
        rsi_prev = float(rsi.iloc[-3])

        if trend == 'UP':
            # 回調觸及 EMA20 + 收回上方 + RSI 從低位翻揚
            touched = low <= ema_now * (1 + self.sl_buffer)
            recovered = close > ema_now
            rsi_turn = rsi_prev < self.rsi_oversold and rsi_now > rsi_prev
            if touched and recovered and rsi_turn:
                self._last_sl_distance = abs(close - low * (1 - self.sl_buffer)) / close
                return 'LONG'
        elif trend == 'DOWN':
            touched = high >= ema_now * (1 - self.sl_buffer)
            broken  = close < ema_now
            rsi_turn = rsi_prev > self.rsi_overbought and rsi_now < rsi_prev
            if touched and broken and rsi_turn:
                self._last_sl_distance = abs(high * (1 + self.sl_buffer) - close) / close
                return 'SHORT'

        return 'HOLD'

    def get_stop_loss(self, entry_price: float, signal: str, symbol: str = '') -> float:
        # 用最近一筆訊號計算的 SL 距離
        d = self._last_sl_distance if self._last_sl_distance > 0 else 0.01
        if signal == 'LONG':
            return round(entry_price * (1 - d), 4)
        else:
            return round(entry_price * (1 + d), 4)

    def get_take_profit(self, entry_price: float, signal: str, symbol: str = '') -> float:
        d = self._last_sl_distance if self._last_sl_distance > 0 else 0.01
        if signal == 'LONG':
            return round(entry_price * (1 + d * self.tp_r_multiple), 4)
        else:
            return round(entry_price * (1 - d * self.tp_r_multiple), 4)


# ────────────────────────────────────────────────────────────────────
# 策略 A 1.1 版：TrendPullbackV11 — 加 ADX Regime Filter
# ────────────────────────────────────────────────────────────────────

class TrendPullbackV11(BaseStrategy):
    """
    V1.1 — 跟 V1 一樣，但加 4H ADX 過濾

    Regime Filter:
      - 4H ADX > adx_min (預設 25) 才交易 → 確認強趨勢
      - ADX <= 25 → HOLD (盤整時不進場)

    其他參數跟 V1 完全一樣（RSI 40/60、TP 1:2、動態 SL）
    """

    def __init__(self, settings: dict | None = None):
        cfg = (settings or {}).get('strategy', {}).get('trend_pullback_v11', {})
        self.ema_short      = cfg.get('ema_short', 20)
        self.ema_long       = cfg.get('ema_long',  50)
        self.rsi_oversold   = cfg.get('rsi_oversold',   40)
        self.rsi_overbought = cfg.get('rsi_overbought', 60)
        self.adx_min        = cfg.get('adx_min', 25)
        self.tp_r_multiple  = cfg.get('tp_r_multiple', 2.0)
        self.sl_buffer      = cfg.get('sl_buffer', 0.005)
        self._last_sl_distance = 0.0
        self._df_4h = None

    def update_data(self, df_4h: pd.DataFrame, df_1d=None):
        self._df_4h = df_4h

    def calculate_signals(self, df: pd.DataFrame, symbol: str = '') -> str:
        if df is None or len(df) < max(self.ema_long, 20) + 5:
            return 'HOLD'
        if self._df_4h is None or len(self._df_4h) < self.ema_long + 5:
            return 'HOLD'

        # 4H 趨勢 + ADX 過濾
        e20_4h = _ema(self._df_4h['close'], self.ema_short)
        e50_4h = _ema(self._df_4h['close'], self.ema_long)
        adx_4h = _adx(self._df_4h, 14)
        if pd.isna(e20_4h.iloc[-2]) or pd.isna(e50_4h.iloc[-2]) or pd.isna(adx_4h.iloc[-2]):
            return 'HOLD'

        # ★ Regime Filter：弱趨勢直接不進場
        if adx_4h.iloc[-2] < self.adx_min:
            return 'HOLD'

        trend = 'UP' if e20_4h.iloc[-2] > e50_4h.iloc[-2] else 'DOWN'

        # 1H 指標（與 V1 同）
        e20 = _ema(df['close'], self.ema_short)
        rsi = _rsi(df['close'], 14)
        last  = df.iloc[-2]
        if pd.isna(rsi.iloc[-2]) or pd.isna(rsi.iloc[-3]) or pd.isna(e20.iloc[-2]):
            return 'HOLD'

        ema_now = e20.iloc[-2]
        close   = float(last['close'])
        low     = float(last['low'])
        high    = float(last['high'])
        rsi_now  = float(rsi.iloc[-2])
        rsi_prev = float(rsi.iloc[-3])

        if trend == 'UP':
            touched = low <= ema_now * (1 + self.sl_buffer)
            recovered = close > ema_now
            rsi_turn = rsi_prev < self.rsi_oversold and rsi_now > rsi_prev
            if touched and recovered and rsi_turn:
                self._last_sl_distance = abs(close - low * (1 - self.sl_buffer)) / close
                return 'LONG'
        elif trend == 'DOWN':
            touched = high >= ema_now * (1 - self.sl_buffer)
            broken  = close < ema_now
            rsi_turn = rsi_prev > self.rsi_overbought and rsi_now < rsi_prev
            if touched and broken and rsi_turn:
                self._last_sl_distance = abs(high * (1 + self.sl_buffer) - close) / close
                return 'SHORT'

        return 'HOLD'

    def get_stop_loss(self, entry_price: float, signal: str, symbol: str = '') -> float:
        d = self._last_sl_distance if self._last_sl_distance > 0 else 0.01
        return round(entry_price * (1 - d), 4) if signal == 'LONG' else round(entry_price * (1 + d), 4)

    def get_take_profit(self, entry_price: float, signal: str, symbol: str = '') -> float:
        d = self._last_sl_distance if self._last_sl_distance > 0 else 0.01
        if signal == 'LONG':
            return round(entry_price * (1 + d * self.tp_r_multiple), 4)
        return round(entry_price * (1 - d * self.tp_r_multiple), 4)


# ────────────────────────────────────────────────────────────────────
# V1-CROSS-100X — RICK 修正版風控
# - SL_distance ≤ 0.5% 才接受（過大就跳過）
# - 額外提供 calculate_signal_with_score() 給 portfolio engine 排序
# ────────────────────────────────────────────────────────────────────

class TrendPullbackCross100x(BaseStrategy):
    """
    V1-CROSS-100X：V1 的風控加強版

    主要改動：
      1. SL_distance > sl_max_pct（預設 0.5%）→ 直接 HOLD（避開大波動進場）
      2. 新增 calculate_signal_with_score() → 訊號品質分數，給 portfolio 排序
      3. 4H ADX、RSI 距離極值的乾淨度都納入 Score
    """

    def __init__(self, settings: dict | None = None):
        cfg = (settings or {}).get('strategy', {}).get('trend_pullback_cross', {})
        self.ema_short      = cfg.get('ema_short', 20)
        self.ema_long       = cfg.get('ema_long',  50)
        self.rsi_oversold   = cfg.get('rsi_oversold',   40)
        self.rsi_overbought = cfg.get('rsi_overbought', 60)
        self.tp_r_multiple  = cfg.get('tp_r_multiple', 2.0)
        self.sl_buffer      = cfg.get('sl_buffer', 0.005)
        self.sl_max_pct     = cfg.get('sl_max_pct', 0.005)   # ★ RICK 規定 ≤ 0.5%
        self._last_sl_distance = 0.0
        self._df_4h = None

    def update_data(self, df_4h, df_1d=None):
        self._df_4h = df_4h

    def _compute_indicators(self, df):
        """計算所有需要的指標，回傳 dict 或 None"""
        if df is None or len(df) < max(self.ema_long, 20) + 5:
            return None
        if self._df_4h is None or len(self._df_4h) < self.ema_long + 5:
            return None

        e20_4h = _ema(self._df_4h['close'], self.ema_short)
        e50_4h = _ema(self._df_4h['close'], self.ema_long)
        adx_4h = _adx(self._df_4h, 14)
        if any(pd.isna(x.iloc[-2]) for x in [e20_4h, e50_4h, adx_4h]):
            return None

        e20 = _ema(df['close'], self.ema_short)
        rsi = _rsi(df['close'], 14)
        if any(pd.isna(x.iloc[-2]) for x in [rsi, e20]) or pd.isna(rsi.iloc[-3]):
            return None

        last = df.iloc[-2]
        return {
            'trend':    'UP' if e20_4h.iloc[-2] > e50_4h.iloc[-2] else 'DOWN',
            'adx_4h':   float(adx_4h.iloc[-2]),
            'ema_now':  float(e20.iloc[-2]),
            'close':    float(last['close']),
            'open':     float(last['open']),
            'low':      float(last['low']),
            'high':     float(last['high']),
            'volume':   float(last['volume']),
            'rsi_now':  float(rsi.iloc[-2]),
            'rsi_prev': float(rsi.iloc[-3]),
            'avg_vol':  float(df['volume'].iloc[-22:-2].mean()),
        }

    def calculate_signals(self, df, symbol=''):
        ind = self._compute_indicators(df)
        if ind is None:
            return 'HOLD'
        sig, _, _ = self._evaluate(ind)
        return sig

    def calculate_signal_with_score(self, df, symbol=''):
        """回傳 (signal, score, sl_distance)；HOLD 時 score=0"""
        ind = self._compute_indicators(df)
        if ind is None:
            return 'HOLD', 0.0, 0.0
        return self._evaluate(ind)

    def _evaluate(self, ind):
        """從 indicators 算訊號 + 分數 + SL 距離"""
        trend = ind['trend']
        ema_now = ind['ema_now']
        close, low, high = ind['close'], ind['low'], ind['high']
        open_  = ind['open']
        rsi_now, rsi_prev = ind['rsi_now'], ind['rsi_prev']

        signal = 'HOLD'
        sl_distance = 0.0

        if trend == 'UP':
            touched = low <= ema_now * (1 + self.sl_buffer)
            recovered = close > ema_now
            rsi_turn = rsi_prev < self.rsi_oversold and rsi_now > rsi_prev
            if touched and recovered and rsi_turn:
                signal = 'LONG'
                sl_distance = abs(close - low * (1 - self.sl_buffer)) / close
        elif trend == 'DOWN':
            touched = high >= ema_now * (1 - self.sl_buffer)
            broken  = close < ema_now
            rsi_turn = rsi_prev > self.rsi_overbought and rsi_now < rsi_prev
            if touched and broken and rsi_turn:
                signal = 'SHORT'
                sl_distance = abs(high * (1 + self.sl_buffer) - close) / close

        if signal == 'HOLD':
            return 'HOLD', 0.0, 0.0

        # ★ SL 過大過濾
        if sl_distance > self.sl_max_pct:
            return 'HOLD', 0.0, sl_distance

        self._last_sl_distance = sl_distance

        # ── Score 計算（高分 = 訊號品質好）──
        # 1. ADX 越高分越高（趨勢越強）
        adx_score = min(ind['adx_4h'] / 40.0, 1.0)  # ADX 40 = 滿分

        # 2. RSI 越極端越乾淨（從 30/70 反彈比從 40/60 反彈乾淨）
        if signal == 'LONG':
            rsi_score = max(0, (self.rsi_oversold - rsi_prev) / self.rsi_oversold)
        else:
            rsi_score = max(0, (rsi_prev - self.rsi_overbought) / (100 - self.rsi_overbought))
        rsi_score = min(rsi_score, 1.0)

        # 3. SL 越緊分越高（風險越小）
        sl_score = 1.0 - (sl_distance / self.sl_max_pct)  # SL 0% = 1，SL 0.5% = 0

        # 4. K 棒實體乾淨度（越乾淨越好）
        body = abs(close - open_)
        full = high - low
        body_score = (body / full) if full > 0 else 0
        body_score = min(body_score, 1.0)

        # 5. 成交量品質（要有量但不能爆量）
        vol_ratio = ind['volume'] / ind['avg_vol'] if ind['avg_vol'] > 0 else 1
        if vol_ratio < 0.5:
            vol_score = 0
        elif vol_ratio < 3:
            vol_score = 1.0
        else:
            vol_score = 0.5  # 爆量可能是新聞，扣一點

        score = (adx_score * 0.3 + rsi_score * 0.25 + sl_score * 0.25
                 + body_score * 0.1 + vol_score * 0.1)

        return signal, score, sl_distance

    def get_stop_loss(self, entry_price, signal, symbol=''):
        d = self._last_sl_distance if self._last_sl_distance > 0 else 0.005
        return round(entry_price * (1 - d), 4) if signal == 'LONG' else round(entry_price * (1 + d), 4)

    def get_take_profit(self, entry_price, signal, symbol=''):
        d = self._last_sl_distance if self._last_sl_distance > 0 else 0.005
        return round(entry_price * (1 + d * self.tp_r_multiple), 4) if signal == 'LONG' \
               else round(entry_price * (1 - d * self.tp_r_multiple), 4)


# ────────────────────────────────────────────────────────────────────
# V2 三組 SL 變體（依 RICK V2 規格）
# 共用 _evaluate 邏輯，差別在 sl_max_pct
# ────────────────────────────────────────────────────────────────────

class _V2Base(TrendPullbackCross100x):
    """V2 共用基礎，子類覆寫 sl_max_pct"""
    def __init__(self, settings, sl_cap, tp_r):
        super().__init__(settings)
        self.sl_max_pct = sl_cap
        self.tp_r_multiple = tp_r


class TrendPullbackV2_SL075(_V2Base):
    """V2-SL-075：SL ≤ 0.75%, TP = 1.5R"""
    def __init__(self, settings=None):
        super().__init__(settings, sl_cap=0.0075, tp_r=1.5)


class TrendPullbackV2_SL100(_V2Base):
    """V2-SL-100：SL ≤ 1.00%, TP = 1.5R"""
    def __init__(self, settings=None):
        super().__init__(settings, sl_cap=0.0100, tp_r=1.5)


class TrendPullbackV2_DYN(_V2Base):
    """V2-DYN：SL 不限上限，但實際風險由 Portfolio Engine 風險預算控制"""
    def __init__(self, settings=None):
        super().__init__(settings, sl_cap=0.0500, tp_r=2.0)  # 5% 等於沒上限（極寬）


# ────────────────────────────────────────────────────────────────────
# V1.2A — ADX 20 + DI 方向確認（放寬 ADX 門檻 + 加 DI 過濾）
# ────────────────────────────────────────────────────────────────────

class TrendPullbackV12A(BaseStrategy):
    """V1.2A：ADX > 20 + +DI/-DI 方向確認。較鬆但有方向性過濾"""

    def __init__(self, settings: dict | None = None):
        cfg = (settings or {}).get('strategy', {}).get('trend_pullback_v12a', {})
        self.ema_short      = cfg.get('ema_short', 20)
        self.ema_long       = cfg.get('ema_long',  50)
        self.rsi_oversold   = cfg.get('rsi_oversold',   40)
        self.rsi_overbought = cfg.get('rsi_overbought', 60)
        self.adx_min        = cfg.get('adx_min', 20)
        self.tp_r_multiple  = cfg.get('tp_r_multiple', 2.0)
        self.sl_buffer      = cfg.get('sl_buffer', 0.005)
        self._last_sl_distance = 0.0
        self._df_4h = None

    def update_data(self, df_4h, df_1d=None):
        self._df_4h = df_4h

    def calculate_signals(self, df, symbol=''):
        if df is None or len(df) < max(self.ema_long, 20) + 5: return 'HOLD'
        if self._df_4h is None or len(self._df_4h) < self.ema_long + 5: return 'HOLD'

        e20_4h = _ema(self._df_4h['close'], self.ema_short)
        e50_4h = _ema(self._df_4h['close'], self.ema_long)
        adx_4h = _adx(self._df_4h, 14)
        # 計算 +DI / -DI
        h, l, c = self._df_4h['high'], self._df_4h['low'], self._df_4h['close']
        plus_dm = h.diff().clip(lower=0)
        minus_dm = (-l.diff()).clip(lower=0)
        plus_dm[plus_dm < minus_dm] = 0
        minus_dm[minus_dm < plus_dm] = 0
        tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
        atr = tr.rolling(14).mean()
        plus_di  = 100 * plus_dm.rolling(14).mean() / atr.replace(0, np.nan)
        minus_di = 100 * minus_dm.rolling(14).mean() / atr.replace(0, np.nan)

        if any(pd.isna(x.iloc[-2]) for x in [e20_4h, e50_4h, adx_4h, plus_di, minus_di]):
            return 'HOLD'
        if adx_4h.iloc[-2] < self.adx_min:
            return 'HOLD'

        di_long  = plus_di.iloc[-2] > minus_di.iloc[-2]
        di_short = minus_di.iloc[-2] > plus_di.iloc[-2]
        trend = 'UP' if e20_4h.iloc[-2] > e50_4h.iloc[-2] else 'DOWN'

        if (trend == 'UP' and not di_long) or (trend == 'DOWN' and not di_short):
            return 'HOLD'  # DI 跟 EMA 不同向 → 不交易

        e20 = _ema(df['close'], self.ema_short)
        rsi = _rsi(df['close'], 14)
        last = df.iloc[-2]
        if any(pd.isna(x.iloc[-2]) for x in [rsi, e20]) or pd.isna(rsi.iloc[-3]):
            return 'HOLD'

        ema_now = e20.iloc[-2]
        close, low, high = float(last['close']), float(last['low']), float(last['high'])
        rsi_now, rsi_prev = float(rsi.iloc[-2]), float(rsi.iloc[-3])

        if trend == 'UP':
            if low <= ema_now * (1 + self.sl_buffer) and close > ema_now \
               and rsi_prev < self.rsi_oversold and rsi_now > rsi_prev:
                self._last_sl_distance = abs(close - low * (1 - self.sl_buffer)) / close
                return 'LONG'
        elif trend == 'DOWN':
            if high >= ema_now * (1 - self.sl_buffer) and close < ema_now \
               and rsi_prev > self.rsi_overbought and rsi_now < rsi_prev:
                self._last_sl_distance = abs(high * (1 + self.sl_buffer) - close) / close
                return 'SHORT'
        return 'HOLD'

    def get_stop_loss(self, entry_price, signal, symbol=''):
        d = self._last_sl_distance if self._last_sl_distance > 0 else 0.01
        return round(entry_price * (1 - d), 4) if signal == 'LONG' else round(entry_price * (1 + d), 4)

    def get_take_profit(self, entry_price, signal, symbol=''):
        d = self._last_sl_distance if self._last_sl_distance > 0 else 0.01
        return round(entry_price * (1 + d * self.tp_r_multiple), 4) if signal == 'LONG' \
               else round(entry_price * (1 - d * self.tp_r_multiple), 4)


# ────────────────────────────────────────────────────────────────────
# V1.2B — 雙時框 ADX（4H + 1D）
# ────────────────────────────────────────────────────────────────────

class TrendPullbackV12B(BaseStrategy):
    """V1.2B：4H ADX > 18 AND 1D ADX > 20 → 雙重確認趨勢"""

    def __init__(self, settings: dict | None = None):
        cfg = (settings or {}).get('strategy', {}).get('trend_pullback_v12b', {})
        self.ema_short      = cfg.get('ema_short', 20)
        self.ema_long       = cfg.get('ema_long',  50)
        self.rsi_oversold   = cfg.get('rsi_oversold',   40)
        self.rsi_overbought = cfg.get('rsi_overbought', 60)
        self.adx_4h_min     = cfg.get('adx_4h_min', 18)
        self.adx_1d_min     = cfg.get('adx_1d_min', 20)
        self.tp_r_multiple  = cfg.get('tp_r_multiple', 2.0)
        self.sl_buffer      = cfg.get('sl_buffer', 0.005)
        self._last_sl_distance = 0.0
        self._df_4h = None
        self._df_1d = None

    def update_data(self, df_4h, df_1d=None):
        self._df_4h = df_4h
        self._df_1d = df_1d

    def calculate_signals(self, df, symbol=''):
        if df is None or len(df) < max(self.ema_long, 20) + 5: return 'HOLD'
        if self._df_4h is None or len(self._df_4h) < self.ema_long + 5: return 'HOLD'
        if self._df_1d is None or len(self._df_1d) < 30: return 'HOLD'

        e20_4h = _ema(self._df_4h['close'], self.ema_short)
        e50_4h = _ema(self._df_4h['close'], self.ema_long)
        adx_4h = _adx(self._df_4h, 14)
        adx_1d = _adx(self._df_1d, 14)
        if any(pd.isna(x.iloc[-2]) for x in [e20_4h, e50_4h, adx_4h]) or pd.isna(adx_1d.iloc[-2]):
            return 'HOLD'

        # 雙時框 ADX 都要過
        if adx_4h.iloc[-2] < self.adx_4h_min or adx_1d.iloc[-2] < self.adx_1d_min:
            return 'HOLD'

        trend = 'UP' if e20_4h.iloc[-2] > e50_4h.iloc[-2] else 'DOWN'

        e20 = _ema(df['close'], self.ema_short)
        rsi = _rsi(df['close'], 14)
        last = df.iloc[-2]
        if any(pd.isna(x.iloc[-2]) for x in [rsi, e20]) or pd.isna(rsi.iloc[-3]):
            return 'HOLD'

        ema_now = e20.iloc[-2]
        close, low, high = float(last['close']), float(last['low']), float(last['high'])
        rsi_now, rsi_prev = float(rsi.iloc[-2]), float(rsi.iloc[-3])

        if trend == 'UP':
            if low <= ema_now * (1 + self.sl_buffer) and close > ema_now \
               and rsi_prev < self.rsi_oversold and rsi_now > rsi_prev:
                self._last_sl_distance = abs(close - low * (1 - self.sl_buffer)) / close
                return 'LONG'
        elif trend == 'DOWN':
            if high >= ema_now * (1 - self.sl_buffer) and close < ema_now \
               and rsi_prev > self.rsi_overbought and rsi_now < rsi_prev:
                self._last_sl_distance = abs(high * (1 + self.sl_buffer) - close) / close
                return 'SHORT'
        return 'HOLD'

    def get_stop_loss(self, entry_price, signal, symbol=''):
        d = self._last_sl_distance if self._last_sl_distance > 0 else 0.01
        return round(entry_price * (1 - d), 4) if signal == 'LONG' else round(entry_price * (1 + d), 4)

    def get_take_profit(self, entry_price, signal, symbol=''):
        d = self._last_sl_distance if self._last_sl_distance > 0 else 0.01
        return round(entry_price * (1 + d * self.tp_r_multiple), 4) if signal == 'LONG' \
               else round(entry_price * (1 - d * self.tp_r_multiple), 4)


# ────────────────────────────────────────────────────────────────────
# V1.2C — ATR 比率過濾（當前 ATR / 30 天均 ATR）
# ────────────────────────────────────────────────────────────────────

class TrendPullbackV12C(BaseStrategy):
    """V1.2C：當前 ATR > 過去 30 天均 ATR × 1.0 才交易（波動率擴張 = 趨勢形成）"""

    def __init__(self, settings: dict | None = None):
        cfg = (settings or {}).get('strategy', {}).get('trend_pullback_v12c', {})
        self.ema_short      = cfg.get('ema_short', 20)
        self.ema_long       = cfg.get('ema_long',  50)
        self.rsi_oversold   = cfg.get('rsi_oversold',   40)
        self.rsi_overbought = cfg.get('rsi_overbought', 60)
        self.atr_ratio_min  = cfg.get('atr_ratio_min', 1.0)  # 當前 ATR > 平均 ATR
        self.atr_lookback   = cfg.get('atr_lookback', 30)    # 30 天 (在 4H 上是 180 根)
        self.tp_r_multiple  = cfg.get('tp_r_multiple', 2.0)
        self.sl_buffer      = cfg.get('sl_buffer', 0.005)
        self._last_sl_distance = 0.0
        self._df_4h = None

    def update_data(self, df_4h, df_1d=None):
        self._df_4h = df_4h

    @staticmethod
    def _atr(df, length=14):
        h, l, c = df['high'], df['low'], df['close']
        tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
        return tr.rolling(length).mean()

    def calculate_signals(self, df, symbol=''):
        if df is None or len(df) < max(self.ema_long, 20) + 5: return 'HOLD'
        # 4H 需要：50（EMA）+ 14（ATR）+ 180（lookback）≈ 244
        atr_lookback_4h = self.atr_lookback * 6  # 30 天 × 6 根/天
        if self._df_4h is None or len(self._df_4h) < atr_lookback_4h + 20:
            return 'HOLD'

        e20_4h = _ema(self._df_4h['close'], self.ema_short)
        e50_4h = _ema(self._df_4h['close'], self.ema_long)
        atr_4h = self._atr(self._df_4h, 14)
        if any(pd.isna(x.iloc[-2]) for x in [e20_4h, e50_4h, atr_4h]):
            return 'HOLD'

        # ATR 比率過濾
        atr_now = float(atr_4h.iloc[-2])
        atr_avg = float(atr_4h.iloc[-atr_lookback_4h - 2:-2].mean())
        if atr_avg <= 0 or atr_now / atr_avg < self.atr_ratio_min:
            return 'HOLD'

        trend = 'UP' if e20_4h.iloc[-2] > e50_4h.iloc[-2] else 'DOWN'

        e20 = _ema(df['close'], self.ema_short)
        rsi = _rsi(df['close'], 14)
        last = df.iloc[-2]
        if any(pd.isna(x.iloc[-2]) for x in [rsi, e20]) or pd.isna(rsi.iloc[-3]):
            return 'HOLD'

        ema_now = e20.iloc[-2]
        close, low, high = float(last['close']), float(last['low']), float(last['high'])
        rsi_now, rsi_prev = float(rsi.iloc[-2]), float(rsi.iloc[-3])

        if trend == 'UP':
            if low <= ema_now * (1 + self.sl_buffer) and close > ema_now \
               and rsi_prev < self.rsi_oversold and rsi_now > rsi_prev:
                self._last_sl_distance = abs(close - low * (1 - self.sl_buffer)) / close
                return 'LONG'
        elif trend == 'DOWN':
            if high >= ema_now * (1 - self.sl_buffer) and close < ema_now \
               and rsi_prev > self.rsi_overbought and rsi_now < rsi_prev:
                self._last_sl_distance = abs(high * (1 + self.sl_buffer) - close) / close
                return 'SHORT'
        return 'HOLD'

    def get_stop_loss(self, entry_price, signal, symbol=''):
        d = self._last_sl_distance if self._last_sl_distance > 0 else 0.01
        return round(entry_price * (1 - d), 4) if signal == 'LONG' else round(entry_price * (1 + d), 4)

    def get_take_profit(self, entry_price, signal, symbol=''):
        d = self._last_sl_distance if self._last_sl_distance > 0 else 0.01
        return round(entry_price * (1 + d * self.tp_r_multiple), 4) if signal == 'LONG' \
               else round(entry_price * (1 - d * self.tp_r_multiple), 4)


# ────────────────────────────────────────────────────────────────────
# 策略 A 改良版：TrendPullbackV2 — 寧缺勿濫
# ────────────────────────────────────────────────────────────────────

class TrendPullbackV2(BaseStrategy):
    """
    趨勢回調 V2 — 提升訊號品質

    改動 vs V1：
      1. RSI 門檻收緊（30/70 vs 40/60）→ 真正極端位置才進場
      2. 4H 加 ADX(14) > 20 過濾 → 確認真趨勢，避開盤整
      3. 進場 K 棒必須有實體確認（收盤價方向 + 紅綠線確認）
      4. 進場 K 棒成交量 > 20 根均量 → 排除無人交易的雜訊
      5. R-multiple 從 2 拉到 3 → 風報比更佳
    """

    def __init__(self, settings: dict | None = None):
        cfg = (settings or {}).get('strategy', {}).get('trend_pullback_v2', {})
        self.ema_short      = cfg.get('ema_short', 20)
        self.ema_long       = cfg.get('ema_long',  50)
        self.rsi_oversold   = cfg.get('rsi_oversold',   30)   # 收緊
        self.rsi_overbought = cfg.get('rsi_overbought', 70)   # 收緊
        self.adx_min        = cfg.get('adx_min',        20)   # 新增
        self.vol_multiplier = cfg.get('vol_multiplier', 1.0)  # 新增
        self.tp_r_multiple  = cfg.get('tp_r_multiple',  3.0)  # 從 2 改 3
        self.sl_buffer      = cfg.get('sl_buffer', 0.005)
        self._last_sl_distance = 0.0
        self._df_4h = None

    def update_data(self, df_4h: pd.DataFrame, df_1d=None):
        self._df_4h = df_4h

    def calculate_signals(self, df: pd.DataFrame, symbol: str = '') -> str:
        if df is None or len(df) < max(self.ema_long, 20) + 5:
            return 'HOLD'
        if self._df_4h is None or len(self._df_4h) < self.ema_long + 5:
            return 'HOLD'

        # 4H 趨勢 + ADX 過濾
        e20_4h = _ema(self._df_4h['close'], self.ema_short)
        e50_4h = _ema(self._df_4h['close'], self.ema_long)
        adx_4h = _adx(self._df_4h, 14)
        if pd.isna(e20_4h.iloc[-2]) or pd.isna(e50_4h.iloc[-2]) or pd.isna(adx_4h.iloc[-2]):
            return 'HOLD'
        if adx_4h.iloc[-2] < self.adx_min:
            return 'HOLD'  # 沒明確趨勢，不交易
        trend = 'UP' if e20_4h.iloc[-2] > e50_4h.iloc[-2] else 'DOWN'

        # 1H 指標
        e20 = _ema(df['close'], self.ema_short)
        rsi = _rsi(df['close'], 14)
        if pd.isna(rsi.iloc[-2]) or pd.isna(rsi.iloc[-3]) or pd.isna(e20.iloc[-2]):
            return 'HOLD'

        last  = df.iloc[-2]
        ema_now = e20.iloc[-2]
        close   = float(last['close'])
        open_   = float(last['open'])
        low     = float(last['low'])
        high    = float(last['high'])
        vol     = float(last['volume'])
        rsi_now  = float(rsi.iloc[-2])
        rsi_prev = float(rsi.iloc[-3])

        # 成交量過濾
        vol_avg = df['volume'].iloc[-22:-2].mean()
        if pd.isna(vol_avg) or vol < vol_avg * self.vol_multiplier:
            return 'HOLD'

        if trend == 'UP':
            touched   = low <= ema_now * (1 + self.sl_buffer)
            recovered = close > ema_now
            rsi_turn  = rsi_prev < self.rsi_oversold and rsi_now > rsi_prev
            bullish   = close > open_   # K 棒實體紅
            if touched and recovered and rsi_turn and bullish:
                self._last_sl_distance = abs(close - low * (1 - self.sl_buffer)) / close
                return 'LONG'
        elif trend == 'DOWN':
            touched = high >= ema_now * (1 - self.sl_buffer)
            broken  = close < ema_now
            rsi_turn = rsi_prev > self.rsi_overbought and rsi_now < rsi_prev
            bearish  = close < open_
            if touched and broken and rsi_turn and bearish:
                self._last_sl_distance = abs(high * (1 + self.sl_buffer) - close) / close
                return 'SHORT'

        return 'HOLD'

    def get_stop_loss(self, entry_price: float, signal: str, symbol: str = '') -> float:
        d = self._last_sl_distance if self._last_sl_distance > 0 else 0.01
        if signal == 'LONG':
            return round(entry_price * (1 - d), 4)
        else:
            return round(entry_price * (1 + d), 4)

    def get_take_profit(self, entry_price: float, signal: str, symbol: str = '') -> float:
        d = self._last_sl_distance if self._last_sl_distance > 0 else 0.01
        if signal == 'LONG':
            return round(entry_price * (1 + d * self.tp_r_multiple), 4)
        else:
            return round(entry_price * (1 - d * self.tp_r_multiple), 4)


# ────────────────────────────────────────────────────────────────────
# 策略 B：VolumeMomentum — 成交量爆量動量
# ────────────────────────────────────────────────────────────────────

class VolumeMomentum(BaseStrategy):
    """
    成交量爆量動量

    進場邏輯：
      1. 1H 成交量 > 最近 20 根平均 × 2.5
      2. 同根 K 棒幅度 |close - open| / open > 0.4%
      3. 順著 K 棒方向進場

    SL: 進場價 × 0.995（多）/ 1.005（空）  ← 0.5% 超緊
    TP: 1:2（即 1%）
    """

    def __init__(self, settings: dict | None = None):
        cfg = (settings or {}).get('strategy', {}).get('volume_momentum', {})
        self.vol_window      = cfg.get('vol_window', 20)
        self.vol_multiplier  = cfg.get('vol_multiplier', 2.5)
        self.body_min_pct    = cfg.get('body_min_pct', 0.004)  # 0.4%
        self.sl_pct          = cfg.get('sl_pct', 0.005)        # 0.5%
        self.tp_r_multiple   = cfg.get('tp_r_multiple', 2.0)

    def update_data(self, df_4h=None, df_1d=None):
        pass  # 不需要多時框

    def calculate_signals(self, df: pd.DataFrame, symbol: str = '') -> str:
        if df is None or len(df) < self.vol_window + 5:
            return 'HOLD'

        last = df.iloc[-2]   # 已收
        vol_avg = df['volume'].iloc[-self.vol_window - 2:-2].mean()

        if pd.isna(vol_avg) or vol_avg <= 0:
            return 'HOLD'

        last_vol = float(last['volume'])
        body_pct = (float(last['close']) - float(last['open'])) / float(last['open'])

        # 條件 1：爆量
        if last_vol < vol_avg * self.vol_multiplier:
            return 'HOLD'

        # 條件 2：方向明確
        if body_pct >= self.body_min_pct:
            return 'LONG'
        elif body_pct <= -self.body_min_pct:
            return 'SHORT'

        return 'HOLD'

    def get_stop_loss(self, entry_price: float, signal: str, symbol: str = '') -> float:
        if signal == 'LONG':
            return round(entry_price * (1 - self.sl_pct), 4)
        else:
            return round(entry_price * (1 + self.sl_pct), 4)

    def get_take_profit(self, entry_price: float, signal: str, symbol: str = '') -> float:
        if signal == 'LONG':
            return round(entry_price * (1 + self.sl_pct * self.tp_r_multiple), 4)
        else:
            return round(entry_price * (1 - self.sl_pct * self.tp_r_multiple), 4)


# ---------------------------------------------------------------------------
# RICK candidate alpha helpers
# ---------------------------------------------------------------------------

def _price_round(price: float) -> float:
    if price >= 1000:
        return round(price, 2)
    if price >= 100:
        return round(price, 3)
    if price >= 1:
        return round(price, 4)
    if price >= 0.01:
        return round(price, 6)
    return round(price, 8)


def _pivot_points(df: pd.DataFrame, left: int = 2, right: int = 2):
    highs, lows = [], []
    if df is None or len(df) < left + right + 3:
        return highs, lows
    for i in range(left, len(df) - right):
        high = float(df['high'].iloc[i])
        low = float(df['low'].iloc[i])
        high_window = df['high'].iloc[i - left:i + right + 1]
        low_window = df['low'].iloc[i - left:i + right + 1]
        if high >= float(high_window.max()):
            highs.append((i, high))
        if low <= float(low_window.min()):
            lows.append((i, low))
    return highs, lows


def _safe_volume_score(volume: float, avg_volume: float) -> float:
    if avg_volume <= 0 or pd.isna(avg_volume):
        return 0.5
    ratio = volume / avg_volume
    if ratio < 0.8:
        return 0.2
    if ratio > 3.0:
        return 0.7
    return min(ratio / 1.5, 1.0)


class Reversal123(BaseStrategy):
    """Quantified 123 reversal pattern."""

    def __init__(self, settings: dict | None = None):
        cfg = (settings or {}).get('strategy', {}).get('reversal_123', {})
        self.lookback = cfg.get('lookback', 140)
        self.pivot_left = cfg.get('pivot_left', 2)
        self.pivot_right = cfg.get('pivot_right', 2)
        self.breakout_tol = cfg.get('breakout_tol', 0.0005)
        self.sl_buffer = cfg.get('sl_buffer', 0.002)
        self.sl_max_pct = cfg.get('sl_max_pct', 0.010)
        self.tp_r_multiple = cfg.get('tp_r_multiple', 1.5)
        self._last_sl_distance = 0.0

    def calculate_signals(self, df: pd.DataFrame, symbol: str = '') -> str:
        sig, _, _ = self.calculate_signal_with_score(df, symbol)
        return sig

    def calculate_signal_with_score(self, df: pd.DataFrame, symbol: str = ''):
        if df is None or len(df) < max(80, self.lookback // 2):
            return 'HOLD', 0.0, 0.0

        work = df.iloc[:-1].tail(self.lookback).copy()
        if len(work) < 50:
            return 'HOLD', 0.0, 0.0

        highs, lows = _pivot_points(work, self.pivot_left, self.pivot_right)
        if len(highs) < 2 or len(lows) < 2:
            return 'HOLD', 0.0, 0.0

        last = work.iloc[-1]
        prev = work.iloc[-2]
        close = float(last['close'])
        avg_vol = float(work['volume'].iloc[-22:-2].mean())
        vol_score = _safe_volume_score(float(last['volume']), avg_vol)

        bullish = self._find_bullish_123(highs, lows, close, float(prev['close']))
        bearish = self._find_bearish_123(highs, lows, close, float(prev['close']))

        if bullish:
            p1, p2, p3 = bullish
            sl_price = p3[1] * (1 - self.sl_buffer)
            sl_dist = (close - sl_price) / close
            if sl_dist <= 0 or sl_dist > self.sl_max_pct:
                return 'HOLD', 0.0, sl_dist
            self._last_sl_distance = sl_dist
            score = self._score_123(close, p1[1], p2[1], p3[1], sl_dist, vol_score, True)
            return 'LONG', score, sl_dist

        if bearish:
            p1, p2, p3 = bearish
            sl_price = p3[1] * (1 + self.sl_buffer)
            sl_dist = (sl_price - close) / close
            if sl_dist <= 0 or sl_dist > self.sl_max_pct:
                return 'HOLD', 0.0, sl_dist
            self._last_sl_distance = sl_dist
            score = self._score_123(close, p1[1], p2[1], p3[1], sl_dist, vol_score, False)
            return 'SHORT', score, sl_dist

        return 'HOLD', 0.0, 0.0

    def _find_bullish_123(self, highs, lows, close, prev_close):
        for p2 in reversed(highs):
            p1s = [p for p in lows if p[0] < p2[0]]
            p3s = [p for p in lows if p2[0] < p[0]]
            if not p1s or not p3s:
                continue
            p1 = p1s[-1]
            p3 = p3s[-1]
            if p3[1] <= p1[1]:
                continue
            if prev_close <= p2[1] and close > p2[1] * (1 + self.breakout_tol):
                return p1, p2, p3
        return None

    def _find_bearish_123(self, highs, lows, close, prev_close):
        for p2 in reversed(lows):
            p1s = [p for p in highs if p[0] < p2[0]]
            p3s = [p for p in highs if p2[0] < p[0]]
            if not p1s or not p3s:
                continue
            p1 = p1s[-1]
            p3 = p3s[-1]
            if p3[1] >= p1[1]:
                continue
            if prev_close >= p2[1] and close < p2[1] * (1 - self.breakout_tol):
                return p1, p2, p3
        return None

    def _score_123(self, close, p1, p2, p3, sl_dist, vol_score, is_long):
        base_range = abs(p2 - p1) / max(abs(p1), 1e-12)
        if is_long:
            structure = max(0.0, min((p3 - p1) / max(abs(p2 - p1), 1e-12), 1.0))
            breakout = max(0.0, min((close - p2) / max(close * sl_dist, 1e-12), 1.0))
        else:
            structure = max(0.0, min((p1 - p3) / max(abs(p1 - p2), 1e-12), 1.0))
            breakout = max(0.0, min((p2 - close) / max(close * sl_dist, 1e-12), 1.0))
        range_score = min(base_range / 0.03, 1.0)
        sl_score = 1.0 - min(sl_dist / self.sl_max_pct, 1.0)
        return structure * 0.30 + breakout * 0.25 + sl_score * 0.25 + vol_score * 0.10 + range_score * 0.10

    def get_stop_loss(self, entry_price: float, signal: str, symbol: str = '') -> float:
        d = self._last_sl_distance if self._last_sl_distance > 0 else self.sl_max_pct
        return _price_round(entry_price * (1 - d)) if signal == 'LONG' else _price_round(entry_price * (1 + d))

    def get_take_profit(self, entry_price: float, signal: str, symbol: str = '') -> float:
        d = self._last_sl_distance if self._last_sl_distance > 0 else self.sl_max_pct
        return _price_round(entry_price * (1 + d * self.tp_r_multiple)) if signal == 'LONG' \
            else _price_round(entry_price * (1 - d * self.tp_r_multiple))


class Fakeout2B(BaseStrategy):
    """Quantified 2B fakeout pattern."""

    def __init__(self, settings: dict | None = None):
        cfg = (settings or {}).get('strategy', {}).get('fakeout_2b', {})
        self.lookback = cfg.get('lookback', 120)
        self.pivot_left = cfg.get('pivot_left', 2)
        self.pivot_right = cfg.get('pivot_right', 2)
        self.breakout_tol = cfg.get('breakout_tol', 0.0005)
        self.sl_buffer = cfg.get('sl_buffer', 0.002)
        self.sl_max_pct = cfg.get('sl_max_pct', 0.010)
        self.tp_r_multiple = cfg.get('tp_r_multiple', 1.5)
        self._last_sl_distance = 0.0

    def calculate_signals(self, df: pd.DataFrame, symbol: str = '') -> str:
        sig, _, _ = self.calculate_signal_with_score(df, symbol)
        return sig

    def calculate_signal_with_score(self, df: pd.DataFrame, symbol: str = ''):
        if df is None or len(df) < 60:
            return 'HOLD', 0.0, 0.0

        work = df.iloc[:-1].tail(self.lookback).copy()
        if len(work) < 40:
            return 'HOLD', 0.0, 0.0

        swings_high, swings_low = _pivot_points(work.iloc[:-1], self.pivot_left, self.pivot_right)
        if not swings_high and not swings_low:
            return 'HOLD', 0.0, 0.0

        last = work.iloc[-1]
        close = float(last['close'])
        high = float(last['high'])
        low = float(last['low'])
        full_range = max(high - low, close * 1e-8)
        avg_vol = float(work['volume'].iloc[-22:-2].mean())
        vol_score = _safe_volume_score(float(last['volume']), avg_vol)

        if swings_low:
            ref_low = swings_low[-1][1]
            if low < ref_low * (1 - self.breakout_tol) and close > ref_low:
                sl_price = low * (1 - self.sl_buffer)
                sl_dist = (close - sl_price) / close
                if 0 < sl_dist <= self.sl_max_pct:
                    self._last_sl_distance = sl_dist
                    rejection = (close - low) / full_range
                    sl_score = 1.0 - min(sl_dist / self.sl_max_pct, 1.0)
                    score = rejection * 0.35 + sl_score * 0.30 + vol_score * 0.20 + 0.15
                    return 'LONG', score, sl_dist

        if swings_high:
            ref_high = swings_high[-1][1]
            if high > ref_high * (1 + self.breakout_tol) and close < ref_high:
                sl_price = high * (1 + self.sl_buffer)
                sl_dist = (sl_price - close) / close
                if 0 < sl_dist <= self.sl_max_pct:
                    self._last_sl_distance = sl_dist
                    rejection = (high - close) / full_range
                    sl_score = 1.0 - min(sl_dist / self.sl_max_pct, 1.0)
                    score = rejection * 0.35 + sl_score * 0.30 + vol_score * 0.20 + 0.15
                    return 'SHORT', score, sl_dist

        return 'HOLD', 0.0, 0.0

    def get_stop_loss(self, entry_price: float, signal: str, symbol: str = '') -> float:
        d = self._last_sl_distance if self._last_sl_distance > 0 else self.sl_max_pct
        return _price_round(entry_price * (1 - d)) if signal == 'LONG' else _price_round(entry_price * (1 + d))

    def get_take_profit(self, entry_price: float, signal: str, symbol: str = '') -> float:
        d = self._last_sl_distance if self._last_sl_distance > 0 else self.sl_max_pct
        return _price_round(entry_price * (1 + d * self.tp_r_multiple)) if signal == 'LONG' \
            else _price_round(entry_price * (1 - d * self.tp_r_multiple))


class RegimeMomentumBreakout(BaseStrategy):
    """Rick V3: BTC/ETH regime + cross-symbol momentum + pullback breakout."""

    def __init__(self, settings: dict | None = None):
        cfg = (settings or {}).get('strategy', {}).get('regime_momentum_breakout', {})
        self.ema_len = cfg.get('ema_len', 20)
        self.rank_top_n = cfg.get('rank_top_n', 3)
        self.pullback_tol = cfg.get('pullback_tol', 0.004)
        self.breakout_bars = cfg.get('breakout_bars', 3)
        self.volume_min = cfg.get('volume_min', 1.0)
        self.sl_buffer = cfg.get('sl_buffer', 0.002)
        self.sl_max_pct = cfg.get('sl_max_pct', 0.010)
        self.tp_r_multiple = cfg.get('tp_r_multiple', 1.5)
        self._last_sl_distance = 0.0
        self._market_context = {'regime': 'NEUTRAL', 'scores': {}}
        self._symbol = ''

    def update_market_context(self, context: dict, symbol: str = ''):
        self._market_context = context or {'regime': 'NEUTRAL', 'scores': {}}
        self._symbol = symbol

    def calculate_signals(self, df: pd.DataFrame, symbol: str = '') -> str:
        sig, _, _ = self.calculate_signal_with_score(df, symbol)
        return sig

    def calculate_signal_with_score(self, df: pd.DataFrame, symbol: str = ''):
        sym = symbol or self._symbol
        if df is None or len(df) < max(80, self.ema_len + 30):
            return 'HOLD', 0.0, 0.0

        ctx = self._market_context or {}
        regime = ctx.get('regime', 'NEUTRAL')
        sym_score = ctx.get('scores', {}).get(sym)
        if regime == 'NEUTRAL' or not sym_score:
            return 'HOLD', 0.0, 0.0

        work = df.iloc[:-1].copy()
        last = work.iloc[-1]
        prev_window = work.iloc[-(self.breakout_bars + 1):-1]
        recent = work.iloc[-8:-1]
        ema = _ema(work['close'], self.ema_len)
        ema_now = float(ema.iloc[-1])
        close = float(last['close'])
        high = float(last['high'])
        low = float(last['low'])
        avg_vol = float(work['volume'].iloc[-22:-2].mean())
        vol_ratio = float(last['volume']) / avg_vol if avg_vol > 0 else 1.0
        if vol_ratio < self.volume_min:
            return 'HOLD', 0.0, 0.0

        if regime == 'LONG':
            if sym_score['long_rank'] > self.rank_top_n:
                return 'HOLD', 0.0, 0.0
            touched = float(recent['low'].min()) <= ema_now * (1 + self.pullback_tol)
            breakout = close > float(prev_window['high'].max())
            if not (touched and breakout):
                return 'HOLD', 0.0, 0.0
            sl_price = min(float(recent['low'].min()), low) * (1 - self.sl_buffer)
            sl_dist = (close - sl_price) / close
            if sl_dist <= 0 or sl_dist > self.sl_max_pct:
                return 'HOLD', 0.0, sl_dist
            self._last_sl_distance = sl_dist
            score = self._score_v3(sym_score['long_score'], close, prev_window['high'].max(),
                                   sl_dist, vol_ratio)
            return 'LONG', score, sl_dist

        if regime == 'SHORT':
            if sym_score['short_rank'] > self.rank_top_n:
                return 'HOLD', 0.0, 0.0
            touched = float(recent['high'].max()) >= ema_now * (1 - self.pullback_tol)
            breakout = close < float(prev_window['low'].min())
            if not (touched and breakout):
                return 'HOLD', 0.0, 0.0
            sl_price = max(float(recent['high'].max()), high) * (1 + self.sl_buffer)
            sl_dist = (sl_price - close) / close
            if sl_dist <= 0 or sl_dist > self.sl_max_pct:
                return 'HOLD', 0.0, sl_dist
            self._last_sl_distance = sl_dist
            score = self._score_v3(sym_score['short_score'], prev_window['low'].min(), close,
                                   sl_dist, vol_ratio)
            return 'SHORT', score, sl_dist

        return 'HOLD', 0.0, 0.0

    def _score_v3(self, rank_score, break_a, break_b, sl_dist, vol_ratio):
        breakout_score = min(abs(float(break_a) - float(break_b)) / max(abs(float(break_b)), 1e-12) / 0.01, 1.0)
        sl_score = 1.0 - min(sl_dist / self.sl_max_pct, 1.0)
        vol_score = min(vol_ratio / 2.0, 1.0)
        return rank_score * 0.50 + breakout_score * 0.20 + sl_score * 0.15 + vol_score * 0.15

    def get_stop_loss(self, entry_price: float, signal: str, symbol: str = '') -> float:
        d = self._last_sl_distance if self._last_sl_distance > 0 else self.sl_max_pct
        return _price_round(entry_price * (1 - d)) if signal == 'LONG' else _price_round(entry_price * (1 + d))

    def get_take_profit(self, entry_price: float, signal: str, symbol: str = '') -> float:
        d = self._last_sl_distance if self._last_sl_distance > 0 else self.sl_max_pct
        return _price_round(entry_price * (1 + d * self.tp_r_multiple)) if signal == 'LONG' \
            else _price_round(entry_price * (1 - d * self.tp_r_multiple))

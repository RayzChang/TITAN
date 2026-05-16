"""
R3 Regime Classifier
====================

Spec   : docs/R3_spec.md §3
Config : config/r3_strategy.yaml `regime`

職責
----
- 接收市場「快照值」+ 系統狀態 + session 狀態
- 根據 §3.1–§3.7 判定 Regime A / B / C / D / UNKNOWN
- 純決策層（state-free）— 不抓資料、不下單、不改持倉

Update frequency（Spec §3.6）由 caller (Sprint 4 router) 決定：
    - A/B 切換：on 4H candle close
    - D1 (market): on 1H candle close or faster
    - D2 (system): realtime
    - C (funding): on funding/premium update
本 module 不強制 — 它只回答「給定這些輸入，現在的 regime 是什麼」。

Priority（Spec §3.7）
    D > C > A > B > UNKNOWN
- D 觸發 → 即使 A/B/C 也成立，最終仍為 D
- C 與 A 同時成立 → 結果為 C，但 `trend_info` 保留 A 方向
- 資料不足關鍵欄位 → UNKNOWN（不硬判）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

import numpy as np
import pandas as pd

from .config_loader import R3Config
from .indicators import (
    can_trade_on_warmup,
    rolling_percentile_rank,
    warmup_policy_from_config,
)


# =============================================================================
# Enums
# =============================================================================
class Regime(str, Enum):
    A_TREND = "A"
    B_SIDEWAYS = "B"
    C_FUNDING_EXTREME = "C"
    D_NO_TRADE = "D"
    UNKNOWN = "UNKNOWN"


REGIME_NAMES: dict[Regime, str] = {
    Regime.A_TREND: "trend",
    Regime.B_SIDEWAYS: "sideways",
    Regime.C_FUNDING_EXTREME: "funding_extreme",
    Regime.D_NO_TRADE: "no_trade",
    Regime.UNKNOWN: "unknown",
}


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"
    CONTRARIAN_LONG = "contrarian_long"
    CONTRARIAN_SHORT = "contrarian_short"
    NONE = "none"


# =============================================================================
# Inputs
# =============================================================================
@dataclass
class SystemStatus:
    """
    Spec §3.4 D2 — 系統可用性 flags。
    Sprint 2 是純資料結構；live trading 由 Sprint 6+ 的監控模組填值。
    """
    api_latency_abnormal: bool = False
    websocket_disconnected: bool = False
    margin_data_unavailable: bool = False
    order_update_unavailable: bool = False


@dataclass
class SessionStatus:
    """
    Spec §3.4 D1 — Session 級虧損控制（由 risk_engine 提供，Sprint 3+）。
    Sprint 2 接受為 optional input；未提供時不會觸發對應 D1。
    """
    daily_pnl_pct: float | None = None       # e.g. -1.5 表示 -1.5%
    consecutive_losses: int | None = None    # e.g. 3 筆連虧


@dataclass
class RegimeSnapshot:
    """
    一個時間點的市場「狀態快照」(market-state values)，
    由 Indicator 層計算並由 caller 組裝。

    Sprint 2 的 classifier 只看這個物件，不直接處理 K 棒。
    """
    # ----- 4H 趨勢過濾（Spec §3.1, §3.2）-----
    ema_4h_short: float | None = None        # EMA50(4H)
    ema_4h_long: float | None = None         # EMA200(4H)
    adx_4h: float | None = None
    atr_4h: float | None = None              # 用於 B regime 「價格在 EMA200 ± 0.5×ATR」

    # ----- 1H 價格與 BB 寬度（Spec §3.2, §5）-----
    close_1h: float | None = None
    bb_width_pct_rank_1h: float | None = None  # 0~100，相對近 90D 的分位

    # ----- 波動率（Spec §3.5, Q5/Q13）-----
    extreme_vol: bool = False                  # 已由 indicators.extreme_vol() 計算
    consecutive_large_candles_triggered: bool = False  # Spec §3.4 D1

    # ----- Funding / Premium（Spec §3.3, §9）-----
    funding_z: float | None = None
    premium_z: float | None = None
    funding_samples_sufficient: bool = False
    premium_samples_sufficient: bool = False

    # ----- Warmup（Spec §3.5）-----
    bar_index_1h: int = 0                      # 從資料起點算的 1H bar 序號
    bars_per_day_1h: int = 24


# =============================================================================
# Output
# =============================================================================
@dataclass
class RegimeState:
    """Classifier 輸出：完整、可追溯、不只是字串。"""
    as_of: datetime
    symbol: str
    regime: Regime
    regime_name: str                           # 由 REGIME_NAMES[regime] 派生
    direction: str                             # Direction.value
    allow_new_entries: bool
    reason_codes: list[str] = field(default_factory=list)
    metrics_snapshot: dict[str, Any] = field(default_factory=dict)
    insufficient_data_fields: list[str] = field(default_factory=list)
    # 當 C 蓋過 A 時，保留 A 的趨勢方向資訊
    trend_info: dict[str, Any] | None = None


# =============================================================================
# Classifier
# =============================================================================
class RegimeClassifier:
    """
    Pure-function classifier（無內部狀態）。

    使用範例
    --------
    >>> from strategies.r3.config_loader import R3Config
    >>> from strategies.r3.regime import RegimeClassifier, RegimeSnapshot
    >>> cfg = R3Config.load()
    >>> rc = RegimeClassifier(cfg)
    >>> snap = RegimeSnapshot(
    ...     ema_4h_short=100, ema_4h_long=90, adx_4h=30,
    ...     close_1h=100, atr_4h=2,
    ...     extreme_vol=False, funding_z=0.5,
    ... )
    >>> state = rc.classify(datetime.now(timezone.utc), "BTC/USDT:USDT", snap)
    >>> state.regime
    <Regime.A_TREND: 'A'>
    """

    def __init__(self, cfg: R3Config):
        self.cfg = cfg
        self.warmup_policy = warmup_policy_from_config(cfg)

    # -------------------------------------------------------------------------
    # Main entry
    # -------------------------------------------------------------------------
    def classify(
        self,
        as_of: datetime,
        symbol: str,
        snapshot: RegimeSnapshot,
        system_status: SystemStatus | None = None,
        session_status: SessionStatus | None = None,
        missing_data_flags: list[str] | None = None,
    ) -> RegimeState:
        system_status = system_status or SystemStatus()
        session_status = session_status or SessionStatus()
        missing_data_flags = list(missing_data_flags or [])

        # ---------------- D2: system risk (highest priority) ----------------
        d2_reasons = self._d2_reasons(system_status)
        if d2_reasons:
            return self._build_state(
                as_of, symbol, snapshot,
                regime=Regime.D_NO_TRADE,
                direction=Direction.NONE,
                allow=False,
                reasons=d2_reasons,
                insufficient=[],
            )

        # ---------------- D1: market risk ----------------
        d1_reasons = self._d1_reasons(snapshot, session_status, missing_data_flags)
        if d1_reasons:
            return self._build_state(
                as_of, symbol, snapshot,
                regime=Regime.D_NO_TRADE,
                direction=Direction.NONE,
                allow=False,
                reasons=d1_reasons,
                insufficient=[],
            )

        # ---------------- Warmup（Spec §3.5）----------------
        # Day 0~30 不交易，回傳 D（reason 標 WARMUP）
        if not can_trade_on_warmup(snapshot.bar_index_1h, snapshot.bars_per_day_1h, self.warmup_policy):
            return self._build_state(
                as_of, symbol, snapshot,
                regime=Regime.D_NO_TRADE,
                direction=Direction.NONE,
                allow=False,
                reasons=["D1_WARMUP_PERIOD"],
                insufficient=[],
            )

        # ---------------- Insufficient data check ----------------
        # 若 4H 趨勢過濾欄位缺漏，無法判 A/B → UNKNOWN
        insufficient = self._insufficient_data_fields(snapshot)
        critical_missing = {"ema_4h_short", "ema_4h_long", "adx_4h"} & set(insufficient)
        if critical_missing:
            return self._build_state(
                as_of, symbol, snapshot,
                regime=Regime.UNKNOWN,
                direction=Direction.NONE,
                allow=False,
                reasons=["INSUFFICIENT_DATA"],
                insufficient=insufficient,
            )

        # ---------------- C / A / B 判定（Priority: C > A > B）----------------
        c_match, c_direction = self._check_c(snapshot)
        a_match, a_direction = self._check_a(snapshot)
        b_match = self._check_b(snapshot)

        if c_match:
            # C 蓋過 A，但 reason 與 trend_info 保留 A 資訊
            reasons = ["C_FUNDING_EXTREME"]
            trend_info: dict[str, Any] | None = None
            if a_match:
                reasons.append("A_TREND_ALSO_PRESENT")
                trend_info = {"a_direction": a_direction.value}
            return self._build_state(
                as_of, symbol, snapshot,
                regime=Regime.C_FUNDING_EXTREME,
                direction=c_direction,
                allow=True,
                reasons=reasons,
                insufficient=insufficient,
                trend_info=trend_info,
            )

        if a_match:
            return self._build_state(
                as_of, symbol, snapshot,
                regime=Regime.A_TREND,
                direction=a_direction,
                allow=True,
                reasons=["A_TREND"],
                insufficient=insufficient,
            )

        if b_match:
            return self._build_state(
                as_of, symbol, snapshot,
                regime=Regime.B_SIDEWAYS,
                direction=Direction.NEUTRAL,
                allow=True,
                reasons=["B_SIDEWAYS"],
                insufficient=insufficient,
            )

        return self._build_state(
            as_of, symbol, snapshot,
            regime=Regime.UNKNOWN,
            direction=Direction.NONE,
            allow=False,
            reasons=["NO_REGIME_MATCH"],
            insufficient=insufficient,
        )

    # =========================================================================
    # Per-regime checks
    # =========================================================================
    def _d2_reasons(self, ss: SystemStatus) -> list[str]:
        out: list[str] = []
        if ss.api_latency_abnormal:
            out.append("D2_API_LATENCY_ABNORMAL")
        if ss.websocket_disconnected:
            out.append("D2_WEBSOCKET_DISCONNECTED")
        if ss.margin_data_unavailable:
            out.append("D2_MARGIN_DATA_UNAVAILABLE")
        if ss.order_update_unavailable:
            out.append("D2_ORDER_UPDATE_UNAVAILABLE")
        return out

    def _d1_reasons(
        self,
        snap: RegimeSnapshot,
        session: SessionStatus,
        missing_flags: list[str],
    ) -> list[str]:
        """
        蒐集 D 觸發原因。

        A2 設計（BOSS 拍板，2026-05-01）：
            - **市場風險**用 `D1_*` 前綴（純市場資料判定）
            - **Session 風險**用 `D_SESSION_RISK_*` 前綴（由 SessionStatus 傳入）
            - 兩者語意上不同：D1 是 spec §3.4 D1 的市場條件；
              SESSION_RISK 是 risk_engine 的累積虧損狀態，未來可能搬出 classifier
            - 未提供 SessionStatus 時，這兩條不觸發
        """
        out: list[str] = []
        d1 = self.cfg.regime.d1_market

        # ---- 市場風險（D1）----
        if snap.extreme_vol:
            out.append("D1_EXTREME_VOL")
        if snap.consecutive_large_candles_triggered:
            out.append("D1_CONSECUTIVE_LARGE_CANDLES")
        if snap.funding_z is not None and not _is_nan(snap.funding_z):
            if abs(snap.funding_z) > d1.funding_z_abs_panic:
                out.append("D1_FUNDING_PANIC")
        if missing_flags:
            out.append("D1_MISSING_DATA")

        # ---- Session 風險（暫時保留在 classifier，A2）----
        if session.daily_pnl_pct is not None:
            if session.daily_pnl_pct <= d1.daily_loss_pct_stop:
                out.append("D_SESSION_RISK_DAILY_LOSS_LIMIT")
        if session.consecutive_losses is not None:
            if session.consecutive_losses >= d1.consecutive_loss_count_stop:
                out.append("D_SESSION_RISK_CONSECUTIVE_LOSS")
        return out

    def _check_a(self, snap: RegimeSnapshot) -> tuple[bool, Direction]:
        """
        Spec §3.1 + §4.1 條件 1（Regime A 判定，不含進場邏輯）：
        - ADX_4H > a_trend.adx_4h_min
        - EMA50 vs EMA200 排列決定方向
        - extreme_vol == False（D 已先檢查，這裡再次防呆）
        - |funding_z| < a_trend.funding_z_abs_max（避免擁擠）

        A1 設計（BOSS 拍板，2026-05-01）：
            A 與 C **互斥**，邊界在 |funding_z| = 2.5：
              - |funding_z| <  2.5  → A 可成立（trend）
              - |funding_z| >= 2.5  → A 失格，且若 premium 也極端則進 C
            這個邊界**沒有 gap**，故此處用嚴格 `>=` 阻擋 A。
        """
        a = self.cfg.regime.a_trend

        if snap.adx_4h is None or _is_nan(snap.adx_4h) or snap.adx_4h <= a.adx_4h_min:
            return False, Direction.NONE
        if snap.ema_4h_short is None or snap.ema_4h_long is None:
            return False, Direction.NONE
        if _is_nan(snap.ema_4h_short) or _is_nan(snap.ema_4h_long):
            return False, Direction.NONE
        # 已被 D1 抓到的 extreme_vol，這裡再防呆
        if snap.extreme_vol:
            return False, Direction.NONE
        # 擁擠則不進 trend（A1：邊界 `>=` 阻擋，與 C threshold `>=` 對齊互斥）
        if snap.funding_z is not None and not _is_nan(snap.funding_z):
            if abs(snap.funding_z) >= a.funding_z_abs_max:
                return False, Direction.NONE

        if snap.ema_4h_short > snap.ema_4h_long:
            return True, Direction.LONG
        if snap.ema_4h_short < snap.ema_4h_long:
            return True, Direction.SHORT
        return False, Direction.NONE

    def _check_b(self, snap: RegimeSnapshot) -> bool:
        """
        Spec §3.2：盤整盤
        - ADX_4H < b_range.adx_4h_max
        - 價格在 EMA200 ± price_band_atr_4h_mult × ATR_4H
        - BB_width 1H 落在 [pct_low, pct_high]（近 90D）
        - |funding_z| < b_range.funding_z_abs_max（中性）
        """
        b = self.cfg.regime.b_range

        if snap.adx_4h is None or _is_nan(snap.adx_4h) or snap.adx_4h >= b.adx_4h_max:
            return False
        if snap.close_1h is None or snap.ema_4h_long is None or snap.atr_4h is None:
            return False
        if _is_nan(snap.close_1h) or _is_nan(snap.ema_4h_long) or _is_nan(snap.atr_4h):
            return False

        distance = abs(snap.close_1h - snap.ema_4h_long)
        if distance > b.price_band_atr_4h_mult * snap.atr_4h:
            return False

        if snap.bb_width_pct_rank_1h is None or _is_nan(snap.bb_width_pct_rank_1h):
            return False
        if not (b.bb_width_percentile_low <= snap.bb_width_pct_rank_1h <= b.bb_width_percentile_high):
            return False

        if snap.funding_z is not None and not _is_nan(snap.funding_z):
            if abs(snap.funding_z) >= b.funding_z_abs_max:
                return False
        # funding 不足樣本時，funding_z 為 NaN — 視為「不阻擋 B」（B 對 funding 中性需求弱）

        return True

    def _check_c(self, snap: RegimeSnapshot) -> tuple[bool, Direction]:
        """
        Spec §3.3 + §6.1：
        - funding_z 與 premium_z **同方向極端**才觸發
        - 樣本不足時不可觸發（returns False, NONE）

        A1 設計（BOSS 拍板，2026-05-01）：
            A 與 C **互斥**，邊界在 |funding_z| = 2.5：
              - |funding_z| >= 2.5 → C 可觸發（若 premium 也達 ±2.0）
              - |funding_z| <  2.5 → 不算 C extreme
            邊界值 2.5 **優先進 C 不進 A**，故此處用 `>=` / `<=` 觸發。
        """
        c = self.cfg.regime.c_extreme

        if not snap.funding_samples_sufficient or not snap.premium_samples_sufficient:
            return False, Direction.NONE
        if snap.funding_z is None or snap.premium_z is None:
            return False, Direction.NONE
        if _is_nan(snap.funding_z) or _is_nan(snap.premium_z):
            return False, Direction.NONE

        # 同方向極端正：市場過熱 → contrarian short
        if snap.funding_z >= c.funding_z_threshold and snap.premium_z >= c.premium_z_threshold:
            return True, Direction.CONTRARIAN_SHORT
        # 同方向極端負：市場過冷 → contrarian long
        if snap.funding_z <= -c.funding_z_threshold and snap.premium_z <= -c.premium_z_threshold:
            return True, Direction.CONTRARIAN_LONG
        return False, Direction.NONE

    # =========================================================================
    # Helpers
    # =========================================================================
    @staticmethod
    def _insufficient_data_fields(snap: RegimeSnapshot) -> list[str]:
        out: list[str] = []
        for fname in ("ema_4h_short", "ema_4h_long", "adx_4h", "close_1h", "atr_4h"):
            v = getattr(snap, fname)
            if v is None or _is_nan(v):
                out.append(fname)
        if not snap.funding_samples_sufficient:
            out.append("funding_z")
        if not snap.premium_samples_sufficient:
            out.append("premium_z")
        if snap.bb_width_pct_rank_1h is None or _is_nan(snap.bb_width_pct_rank_1h):
            out.append("bb_width_pct_rank_1h")
        return out

    def _build_state(
        self,
        as_of: datetime,
        symbol: str,
        snapshot: RegimeSnapshot,
        *,
        regime: Regime,
        direction: Direction,
        allow: bool,
        reasons: list[str],
        insufficient: list[str],
        trend_info: dict[str, Any] | None = None,
    ) -> RegimeState:
        return RegimeState(
            as_of=as_of,
            symbol=symbol,
            regime=regime,
            regime_name=REGIME_NAMES[regime],
            direction=direction.value,
            allow_new_entries=allow,
            reason_codes=list(reasons),
            metrics_snapshot=_snapshot_to_dict(snapshot),
            insufficient_data_fields=list(insufficient),
            trend_info=trend_info,
        )


# =============================================================================
# Snapshot builder (helper) — 從 Indicator-attached DFs 組裝快照
# =============================================================================
def build_snapshot_from_indicators(
    cfg: R3Config,
    df_4h_with_indicators: pd.DataFrame,
    df_1h_with_indicators: pd.DataFrame,
    funding_z_value: float | None,
    premium_z_value: float | None,
    extreme_vol_at_t: bool,
    consecutive_large_candles_at_t: bool,
    bar_index_1h: int,
    bars_per_day_1h: int = 24,
) -> RegimeSnapshot:
    """
    從已附指標的 4H / 1H DF 取「最後一根」的值，組成 RegimeSnapshot。

    呼叫者責任：
    - df_4h 必須已跑過 attach_core_indicators(df, cfg, '4h')
      （含 ema_50 / ema_200 / adx_14 / atr_14）
    - df_1h 必須已跑過 attach_core_indicators(df, cfg, '1h')
      （含 bb_width / atr_pct / ...）
    - funding_z_value / premium_z_value 由呼叫者用 indicators.funding_z()
      / premium_z() 算好後傳入（單值，最新）
    - extreme_vol_at_t 由 indicators.extreme_vol() 取最後一根布林值
    - consecutive_large_candles_at_t 用 indicators.consecutive_large_candles_count()
      檢查最近 N 根是否全部大於 multiplier × ATR
    """
    tfi = cfg.regime.trend_filter_indicators
    atr_period = cfg.realized_vol.atr_period

    last_4h = df_4h_with_indicators.iloc[-1] if len(df_4h_with_indicators) > 0 else None
    last_1h = df_1h_with_indicators.iloc[-1] if len(df_1h_with_indicators) > 0 else None

    ema_short = _last_or_none(last_4h, f"ema_{tfi.ema_short_period}")
    ema_long = _last_or_none(last_4h, f"ema_{tfi.ema_long_period}")
    adx_v = _last_or_none(last_4h, f"adx_{tfi.adx_period}")
    atr_4h_v = _last_or_none(last_4h, f"atr_{atr_period}")
    close_1h_v = _last_or_none(last_1h, "close")

    # BB width percentile rank（rolling 90D × 24 bars/day = 2160 視窗）
    bb_width_pct_rank: float | None = None
    if "bb_width_pct_rank_1h" in df_1h_with_indicators.columns:
        value = _last_or_none(last_1h, "bb_width_pct_rank_1h")
        if value is not None and not _is_nan(value):
            bb_width_pct_rank = value
    elif "bb_width" in df_1h_with_indicators.columns:
        lookback_bars = cfg.regime.b_range.bb_width_percentile_lookback_days * 24
        ranks = rolling_percentile_rank(df_1h_with_indicators["bb_width"], window=lookback_bars)
        if len(ranks) > 0 and not _is_nan(ranks.iloc[-1]):
            bb_width_pct_rank = float(ranks.iloc[-1])

    funding_sufficient = funding_z_value is not None and not _is_nan(funding_z_value)
    premium_sufficient = premium_z_value is not None and not _is_nan(premium_z_value)

    return RegimeSnapshot(
        ema_4h_short=ema_short,
        ema_4h_long=ema_long,
        adx_4h=adx_v,
        atr_4h=atr_4h_v,
        close_1h=close_1h_v,
        bb_width_pct_rank_1h=bb_width_pct_rank,
        extreme_vol=extreme_vol_at_t,
        consecutive_large_candles_triggered=consecutive_large_candles_at_t,
        funding_z=funding_z_value,
        premium_z=premium_z_value,
        funding_samples_sufficient=funding_sufficient,
        premium_samples_sufficient=premium_sufficient,
        bar_index_1h=bar_index_1h,
        bars_per_day_1h=bars_per_day_1h,
    )


# =============================================================================
# Internal utilities
# =============================================================================
def _is_nan(x: Any) -> bool:
    """安全的 NaN 判斷（None / non-float 不算 NaN）。"""
    if x is None:
        return False
    if isinstance(x, float):
        return np.isnan(x)
    if isinstance(x, (int, np.integer)):
        return False
    try:
        return bool(pd.isna(x))
    except Exception:
        return False


def _last_or_none(row: pd.Series | None, col: str) -> float | None:
    if row is None:
        return None
    if col not in row.index:
        return None
    v = row[col]
    if v is None or _is_nan(v):
        return None
    return float(v)


def _snapshot_to_dict(snap: RegimeSnapshot) -> dict[str, Any]:
    return {
        "ema_4h_short": snap.ema_4h_short,
        "ema_4h_long": snap.ema_4h_long,
        "adx_4h": snap.adx_4h,
        "atr_4h": snap.atr_4h,
        "close_1h": snap.close_1h,
        "bb_width_pct_rank_1h": snap.bb_width_pct_rank_1h,
        "extreme_vol": snap.extreme_vol,
        "consecutive_large_candles_triggered": snap.consecutive_large_candles_triggered,
        "funding_z": snap.funding_z,
        "premium_z": snap.premium_z,
        "funding_samples_sufficient": snap.funding_samples_sufficient,
        "premium_samples_sufficient": snap.premium_samples_sufficient,
        "bar_index_1h": snap.bar_index_1h,
        "bars_per_day_1h": snap.bars_per_day_1h,
    }

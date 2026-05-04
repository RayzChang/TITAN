"""
R3 Sprint 2 Real-Data Regime Smoke Test
========================================

依 BOSS 指示驗證 Regime classifier 在真實 BTC / ETH 資料上的行為。

範圍
----
- 用 R3DataLoader / R3ExchangeData / indicators / regime 整套
- 抓最近 ~100 天歷史（含暖機 + funding 90D + EMA200 4H）
- 對最近 7 天逐根 1H bar 跑 classifier
- 統計 7 天內 A/B/C/D/UNKNOWN 出現次數
- 印出每個 symbol 的最新一筆 RegimeState

不做的事
--------
- 不下單
- 不產生交易訊號
- 不寫回測
- 不 forward-fill K 棒
- 不假造資料

執行：
    .venv/Scripts/python tools/r3_regime_smoke.py
"""
from __future__ import annotations

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from strategies.r3.config_loader import R3Config
from strategies.r3 import indicators as ind
from strategies.r3.data_loader import R3DataLoader, _default_cache_dir
from strategies.r3.exchange import R3ExchangeData
from strategies.r3.regime import (
    Regime,
    RegimeClassifier,
    RegimeState,
    build_snapshot_from_indicators,
)


SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
HISTORY_DAYS = 100   # 暖機 + funding 90D + EMA200(4H, 200×4h ≈ 33d) + ATR
WINDOW_DAYS = 7      # Regime 統計窗口
KLINES_TIMEFRAME = "1h"


def fmt_dt(dt) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%Y-%m-%d %H:%M") if hasattr(dt, "strftime") else str(dt)


def _ensure_utc(ts) -> datetime:
    """把任何時間型別正規化成 UTC-aware datetime"""
    if hasattr(ts, "to_pydatetime"):
        ts = ts.to_pydatetime()
    if isinstance(ts, datetime) and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def fetch_all_data(cfg: R3Config, end: datetime, cache_dir: Path):
    """抓 100 天 1h/4h/funding/premium，順手抓 7 天 5m（驗 fetch 可用）"""
    loader = R3DataLoader(cfg, cache_dir=cache_dir)
    ex_data = R3ExchangeData(cfg, cache_dir=cache_dir)

    history_start = end - timedelta(days=HISTORY_DAYS)
    short_start = end - timedelta(days=WINDOW_DAYS)

    print(f"  History range : {fmt_dt(history_start)} → {fmt_dt(end)} ({HISTORY_DAYS} days)")
    print(f"  Window range  : {fmt_dt(short_start)} → {fmt_dt(end)} ({WINDOW_DAYS} days)")
    print()

    data = {}
    for sym in SYMBOLS:
        print(f"  Fetching {sym} ...")
        t0 = time.time()
        # 5m: 7 天（Sprint 2 classifier 不直接用，但 BOSS 指定要驗 fetch 可用）
        df_5m = loader.load_ohlcv(sym, "5m", start=short_start, end=end)
        # 1h / 4h: 100 天
        df_1h = loader.load_ohlcv(sym, "1h", start=history_start, end=end)
        df_4h = loader.load_ohlcv(sym, "4h", start=history_start, end=end)
        # funding: 100 天
        df_funding = ex_data.fetch_funding_history(sym, start=history_start, end=end)
        # mark / index / premium: 100 天 @ 1h
        df_mark = ex_data.fetch_mark_price_klines(sym, KLINES_TIMEFRAME, start=history_start, end=end)
        df_index = ex_data.fetch_index_price_klines(sym, KLINES_TIMEFRAME, start=history_start, end=end)
        df_premium = ex_data.fetch_premium_index_klines(sym, KLINES_TIMEFRAME, start=history_start, end=end)
        elapsed = time.time() - t0

        data[sym] = {
            "5m": df_5m, "1h": df_1h, "4h": df_4h,
            "funding": df_funding,
            "mark": df_mark, "index": df_index, "premium": df_premium,
        }
        print(f"    [{elapsed:.1f}s] "
              f"5m={len(df_5m):>4}  1h={len(df_1h):>5}  4h={len(df_4h):>4}  "
              f"funding={len(df_funding):>3}  premium={len(df_premium):>4}")

    api_limits = loader.api_limits + ex_data.api_limits
    return data, api_limits


def compute_indicator_series(cfg: R3Config, sym: str, dfs: dict):
    """把 raw OHLCV / funding / premium 算成可供 classifier 用的指標序列。"""
    df_1h = dfs["1h"]
    df_4h = dfs["4h"]
    df_funding = dfs["funding"]
    df_premium = dfs["premium"]

    # 附 1h / 4h 指標（attach_core_indicators 會用 config 的 period）
    df_1h_ind = ind.attach_core_indicators(df_1h, cfg, "1h")
    df_4h_ind = ind.attach_core_indicators(df_4h, cfg, "4h")

    atr_period = cfg.realized_vol.atr_period

    # extreme_vol per 1h bar — 三段式 warmup（Q5/Q13）
    warmup_pol = ind.warmup_policy_from_config(cfg)
    atr_pct_col = f"atr_pct_{atr_period}"
    extreme_vol_series = ind.extreme_vol(
        df_1h_ind[atr_pct_col].fillna(0.0),
        bars_per_day=24,
        policy=warmup_pol,
    )

    # consecutive large candles — 1h
    d1cfg = cfg.regime.d1_market
    atr_col = f"atr_{atr_period}"
    clc_count = ind.consecutive_large_candles_count(
        df_1h_ind["high"], df_1h_ind["low"], df_1h_ind[atr_col],
        multiplier=d1cfg.large_candle_atr_mult,
        n_recent=d1cfg.consecutive_large_candles,
    )
    clc_triggered = (clc_count >= d1cfg.consecutive_large_candles).fillna(False)

    # BB width percentile rank — 90D × 24 bars/day = 2160 bars
    lookback_bars = cfg.regime.b_range.bb_width_percentile_lookback_days * 24
    bb_pct_rank = ind.rolling_percentile_rank(df_1h_ind["bb_width"], window=lookback_bars)
    df_1h_ind["bb_width_pct_rank"] = bb_pct_rank

    # funding_z — 在 funding event timeline 上計算，再 forward-fill 到 1h bar
    funding_z_events = pd.Series(dtype=float)
    if len(df_funding) > 0:
        funding_z_events = ind.funding_z(
            df_funding["funding_rate"],
            lookback_days=cfg.funding.lookback_days,
            funding_interval_hours=cfg.funding.default_interval_hours,
            min_samples=cfg.funding.min_samples_required,
        )
    # ffill 到 1h index（funding 事件較稀疏：8h 一次）
    funding_z_1h = (
        funding_z_events.reindex(df_1h_ind.index, method="ffill")
        if len(funding_z_events) > 0
        else pd.Series(index=df_1h_ind.index, dtype=float)
    )

    # premium_z — 直接在 premium klines 上計算（1h granularity）
    premium_window_bars = lookback_bars   # 同 90D × 24
    premium_min_samples = cfg.funding.min_samples_required
    premium_z_series = pd.Series(dtype=float)
    if len(df_premium) > 0:
        premium_z_series = ind.premium_z(
            df_premium["close"],
            window=premium_window_bars,
            min_samples=premium_min_samples,
        )
    premium_z_1h = (
        premium_z_series.reindex(df_1h_ind.index, method="ffill")
        if len(premium_z_series) > 0
        else pd.Series(index=df_1h_ind.index, dtype=float)
    )

    return {
        "df_1h_ind": df_1h_ind,
        "df_4h_ind": df_4h_ind,
        "extreme_vol": extreme_vol_series,
        "clc_triggered": clc_triggered,
        "funding_z_1h": funding_z_1h,
        "premium_z_1h": premium_z_1h,
    }


def classify_window(cfg: R3Config, sym: str, ind_data: dict, end: datetime):
    """對最近 7 天的每根 1h bar 跑 classifier，回 (counts, last_state, list_of_states)"""
    rc = RegimeClassifier(cfg)
    df_1h_ind = ind_data["df_1h_ind"]
    df_4h_ind = ind_data["df_4h_ind"]
    extreme_vol_series = ind_data["extreme_vol"]
    clc_triggered = ind_data["clc_triggered"]
    funding_z_1h = ind_data["funding_z_1h"]
    premium_z_1h = ind_data["premium_z_1h"]

    window_start = end - timedelta(days=WINDOW_DAYS)
    recent_index = df_1h_ind.index[df_1h_ind.index >= window_start]

    counts: Counter = Counter()
    states: list[RegimeState] = []
    last_state: RegimeState | None = None

    for ts in recent_index:
        ts_utc = _ensure_utc(ts)

        # 該 ts 之前最後一根 4h bar
        df_4h_at_or_before = df_4h_ind.loc[df_4h_ind.index <= ts]
        if df_4h_at_or_before.empty:
            continue
        df_1h_at_or_before = df_1h_ind.loc[df_1h_ind.index <= ts]

        funding_z_value = funding_z_1h.get(ts)
        if funding_z_value is not None and pd.isna(funding_z_value):
            funding_z_value = None
        premium_z_value = premium_z_1h.get(ts)
        if premium_z_value is not None and pd.isna(premium_z_value):
            premium_z_value = None

        ev = extreme_vol_series.get(ts, False)
        ev = bool(ev) if not pd.isna(ev) else False
        clc = clc_triggered.get(ts, False)
        clc = bool(clc) if not pd.isna(clc) else False

        snap = build_snapshot_from_indicators(
            cfg=cfg,
            df_4h_with_indicators=df_4h_at_or_before,
            df_1h_with_indicators=df_1h_at_or_before,
            funding_z_value=funding_z_value,
            premium_z_value=premium_z_value,
            extreme_vol_at_t=ev,
            consecutive_large_candles_at_t=clc,
            bar_index_1h=df_1h_ind.index.get_loc(ts),
            bars_per_day_1h=24,
        )
        state = rc.classify(ts_utc, sym, snap)
        states.append(state)
        counts[state.regime.value] += 1
        last_state = state

    return counts, last_state, states


def validate_state(state: RegimeState) -> list[str]:
    """RegimeState 結構自動體檢；回傳 issue 列表（空表示 OK）"""
    issues: list[str] = []
    required = [
        "as_of", "symbol", "regime", "regime_name",
        "direction", "allow_new_entries",
        "reason_codes", "metrics_snapshot", "insufficient_data_fields",
    ]
    for f in required:
        if not hasattr(state, f):
            issues.append(f"missing field {f}")

    if not state.reason_codes:
        issues.append("reason_codes is empty")

    # 不應該有交易副作用
    forbidden = ["create_order", "submit_order", "open_position", "close_position",
                 "place_stop", "place_take_profit"]
    for fn in forbidden:
        if hasattr(state, fn):
            issues.append(f"unexpected trading attr `{fn}` on RegimeState")
    return issues


def print_state(label: str, state: RegimeState | None):
    if state is None:
        print(f"  {label}: (no state — empty window)")
        return
    print(f"  {label}")
    print(f"    timestamp           : {state.as_of}")
    print(f"    regime              : {state.regime.value} ({state.regime_name})")
    print(f"    direction           : {state.direction}")
    print(f"    allow_new_entries   : {state.allow_new_entries}")
    print(f"    reason_codes        : {state.reason_codes}")
    if state.insufficient_data_fields:
        print(f"    insufficient_data   : {state.insufficient_data_fields}")
    if state.trend_info:
        print(f"    trend_info (C-over-A): {state.trend_info}")
    snap = state.metrics_snapshot
    print(f"    snapshot:")
    for k in ["ema_4h_short", "ema_4h_long", "adx_4h", "atr_4h",
              "close_1h", "bb_width_pct_rank_1h",
              "funding_z", "premium_z",
              "funding_samples_sufficient", "premium_samples_sufficient",
              "extreme_vol", "consecutive_large_candles_triggered"]:
        v = snap.get(k)
        if isinstance(v, float):
            print(f"      {k:<35} = {v:.4f}")
        else:
            print(f"      {k:<35} = {v}")


def main():
    print("=" * 80)
    print("  R3 Sprint 2 — Regime Classifier Smoke Test (real BTC / ETH data)")
    print("=" * 80)

    cfg = R3Config.load()
    cache_dir = _default_cache_dir()
    end = datetime.now(timezone.utc)

    print(f"\n  Cache dir: {cache_dir}\n")

    # ---------- Phase 1: fetch ----------
    print("─" * 80)
    print("  Phase 1 — fetch data")
    print("─" * 80)
    data, api_limits = fetch_all_data(cfg, end, cache_dir)

    # ---------- Phase 2: compute indicators ----------
    print("\n" + "─" * 80)
    print("  Phase 2 — compute indicator series")
    print("─" * 80)
    ind_data_per_sym = {}
    for sym in SYMBOLS:
        ind_data_per_sym[sym] = compute_indicator_series(cfg, sym, data[sym])
        bb_rank_last = ind_data_per_sym[sym]["df_1h_ind"]["bb_width_pct_rank"].dropna()
        funding_z_last = ind_data_per_sym[sym]["funding_z_1h"].dropna()
        premium_z_last = ind_data_per_sym[sym]["premium_z_1h"].dropna()
        print(f"  {sym}: "
              f"BB rank valid={len(bb_rank_last):>5}  "
              f"funding_z valid={len(funding_z_last):>5}  "
              f"premium_z valid={len(premium_z_last):>5}")

    # ---------- Phase 3: classify last 7 days ----------
    print("\n" + "─" * 80)
    print(f"  Phase 3 — classify last {WINDOW_DAYS} days (1H bar-by-bar)")
    print("─" * 80)

    overall_counts: Counter = Counter()
    last_states: dict[str, RegimeState | None] = {}
    all_validation_issues: list[str] = []

    for sym in SYMBOLS:
        counts, last_state, states = classify_window(cfg, sym, ind_data_per_sym[sym], end)
        last_states[sym] = last_state
        for k, v in counts.items():
            overall_counts[k] += v

        print(f"\n  {sym}: {sum(counts.values())} bars classified")
        for r in ["A", "B", "C", "D", "UNKNOWN"]:
            n = counts.get(r, 0)
            pct = (n / sum(counts.values()) * 100) if counts.values() else 0
            print(f"    {r:>8} : {n:>4}  ({pct:5.1f}%)")

        # Validate every state
        for st in states:
            issues = validate_state(st)
            if issues:
                all_validation_issues.append(f"{sym} @ {st.as_of}: {issues}")

    # ---------- Phase 4: latest state per symbol ----------
    print("\n" + "─" * 80)
    print("  Phase 4 — latest RegimeState per symbol")
    print("─" * 80)
    for sym in SYMBOLS:
        print()
        print_state(sym, last_states[sym])

    # ---------- Phase 5: validation summary ----------
    print("\n" + "─" * 80)
    print("  Phase 5 — output structure validation")
    print("─" * 80)
    if all_validation_issues:
        print(f"  [FAIL] {len(all_validation_issues)} state validation issues:")
        for issue in all_validation_issues[:10]:
            print(f"    - {issue}")
    else:
        print(f"  [OK] all RegimeState objects pass structure validation")

    # ---------- Phase 6: missing_data_report ----------
    print("\n" + "─" * 80)
    print("  Phase 6 — missing_data_report.md")
    print("─" * 80)
    if api_limits:
        print(f"  [WARN] API limits / errors: {len(api_limits)}")
        for note in api_limits:
            print(f"    - {note}")
    else:
        print(f"  [OK] No API limits / errors")

    # 觸發資料層 missing_data_report 寫入（用 r3_smoke 同規則）
    loader = R3DataLoader(cfg, cache_dir=cache_dir)
    loader._api_limits = list(api_limits)
    loader._integrity_log = []
    report_path = loader.write_missing_data_report()
    print(f"  missing_data_report.md generated: {'YES (' + str(report_path) + ')' if report_path else 'NO'}")

    # ---------- Summary ----------
    print("\n" + "=" * 80)
    print("  Summary")
    print("=" * 80)
    total = sum(overall_counts.values())
    print(f"  Symbols classified           : {len(SYMBOLS)}")
    print(f"  Total 1H bars classified     : {total}  (per symbol ~ {total // max(len(SYMBOLS), 1)})")
    print(f"  Aggregate regime distribution:")
    for r in ["A", "B", "C", "D", "UNKNOWN"]:
        n = overall_counts.get(r, 0)
        pct = (n / total * 100) if total else 0
        print(f"    {r:>8} : {n:>4}  ({pct:5.1f}%)")
    print(f"  RegimeState validation       : "
          f"{'PASS' if not all_validation_issues else 'FAIL ('+str(len(all_validation_issues))+' issues)'}")
    print(f"  API limits                   : {len(api_limits)}")
    print(f"  missing_data_report.md       : {'YES' if report_path else 'NO'}")
    print()


if __name__ == "__main__":
    main()

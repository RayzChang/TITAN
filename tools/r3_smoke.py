"""
R3 Sprint 1 Real-Data Smoke Test
=================================

跑真實 API：BTC/ETH，最近 7 天。

用途
----
- 驗證 ccxt 連線 + pagination + cache 寫入
- 驗證 funding / mark / index / premium 端點實際可拉
- 產生（或確認不產生）missing_data_report.md

執行：
    .venv/Scripts/python tools/r3_smoke.py

注意：會打**真實**幣安 fapi 公開端點（不需要 API key）。
"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from strategies.r3.config_loader import R3Config
from strategies.r3.data_loader import R3DataLoader, _default_cache_dir
from strategies.r3.exchange import R3ExchangeData


SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
TIMEFRAMES_OHLCV = ["5m", "1h", "4h"]
KLINES_TIMEFRAME = "1h"   # mark / index / premium 用 1h

UTC = timezone.utc


def fmt_dt(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "—"


def main():
    print("=" * 80)
    print("  R3 Sprint 1 — Real-Data Smoke Test")
    print("=" * 80)

    cfg = R3Config.load()
    end = datetime.now(UTC)
    start = end - timedelta(days=7)
    print(f"\n  時間範圍: {fmt_dt(start)} → {fmt_dt(end)} (UTC)")

    cache_dir = _default_cache_dir()
    print(f"  Cache dir: {cache_dir}")

    loader = R3DataLoader(cfg, cache_dir=cache_dir)
    ex_data = R3ExchangeData(cfg, cache_dir=cache_dir)

    summary: list[dict] = []

    # =========================================================================
    # 1. OHLCV via R3DataLoader
    # =========================================================================
    print("\n" + "─" * 80)
    print("  Phase 1 — OHLCV (R3DataLoader)")
    print("─" * 80)

    for sym in SYMBOLS:
        for tf in TIMEFRAMES_OHLCV:
            t0 = time.time()
            df = loader.load_ohlcv(sym, tf, start=start, end=end)
            elapsed = time.time() - t0

            cache_path = loader.cache_path(sym, tf)
            integrity = loader.integrity_log[-1] if loader.integrity_log else None

            print(f"  {sym:>20} {tf:>3}: "
                  f"{len(df):>5} bars  "
                  f"({fmt_dt(df.index.min().to_pydatetime() if not df.empty else None)} → "
                  f"{fmt_dt(df.index.max().to_pydatetime() if not df.empty else None)})  "
                  f"clean={integrity.is_clean if integrity else '—'}  "
                  f"{elapsed:.2f}s")

            summary.append({
                "source": "ohlcv",
                "symbol": sym,
                "timeframe": tf,
                "n_bars": len(df),
                "is_sorted": df.index.is_monotonic_increasing if not df.empty else None,
                "n_duplicates": int(df.index.duplicated().sum()) if not df.empty else 0,
                "n_nulls": int(df.isna().any(axis=1).sum()) if not df.empty else 0,
                "n_gaps": integrity.n_gaps if integrity else None,
                "cache_path": str(cache_path),
                "cache_exists": cache_path.exists(),
            })

    # =========================================================================
    # 2. Funding rate history
    # =========================================================================
    print("\n" + "─" * 80)
    print("  Phase 2 — Funding Rate History (R3ExchangeData)")
    print("─" * 80)

    for sym in SYMBOLS:
        t0 = time.time()
        df = ex_data.fetch_funding_history(sym, start=start, end=end)
        elapsed = time.time() - t0

        from strategies.r3.exchange import _funding_cache_path
        cache_path = _funding_cache_path(cache_dir, sym)

        n_dup = int(df.index.duplicated().sum()) if not df.empty else 0
        sorted_ok = df.index.is_monotonic_increasing if not df.empty else None
        n_nulls = int(df[["funding_rate"]].isna().any(axis=1).sum()) if not df.empty else 0

        print(f"  {sym:>20}     : "
              f"{len(df):>5} events ({fmt_dt(df.index.min().to_pydatetime() if not df.empty else None)} → "
              f"{fmt_dt(df.index.max().to_pydatetime() if not df.empty else None)})  "
              f"sorted={sorted_ok}  dup={n_dup}  null={n_nulls}  {elapsed:.2f}s")

        summary.append({
            "source": "funding",
            "symbol": sym,
            "n_bars": len(df),
            "is_sorted": sorted_ok,
            "n_duplicates": n_dup,
            "n_nulls": n_nulls,
            "cache_path": str(cache_path),
            "cache_exists": cache_path.exists(),
        })

    # =========================================================================
    # 3. Mark / Index / Premium klines
    # =========================================================================
    print("\n" + "─" * 80)
    print(f"  Phase 3 — Mark / Index / Premium Klines @ {KLINES_TIMEFRAME}")
    print("─" * 80)

    for sym in SYMBOLS:
        for kind, fn_name in [
            ("mark",     "fetch_mark_price_klines"),
            ("index",    "fetch_index_price_klines"),
            ("premium",  "fetch_premium_index_klines"),
        ]:
            t0 = time.time()
            try:
                df = getattr(ex_data, fn_name)(sym, KLINES_TIMEFRAME, start=start, end=end)
            except Exception as e:
                print(f"  {sym:>20} {kind:>8}: ERROR {type(e).__name__}: {e}")
                summary.append({
                    "source": kind,
                    "symbol": sym,
                    "n_bars": 0,
                    "error": str(e),
                })
                continue
            elapsed = time.time() - t0

            n_dup = int(df.index.duplicated().sum()) if not df.empty else 0
            sorted_ok = df.index.is_monotonic_increasing if not df.empty else None
            n_nulls = int(df.isna().any(axis=1).sum()) if not df.empty else 0

            print(f"  {sym:>20} {kind:>8}: "
                  f"{len(df):>5} bars  ({fmt_dt(df.index.min().to_pydatetime() if not df.empty else None)} → "
                  f"{fmt_dt(df.index.max().to_pydatetime() if not df.empty else None)})  "
                  f"sorted={sorted_ok}  dup={n_dup}  null={n_nulls}  {elapsed:.2f}s")

            summary.append({
                "source": kind,
                "symbol": sym,
                "timeframe": KLINES_TIMEFRAME,
                "n_bars": len(df),
                "is_sorted": sorted_ok,
                "n_duplicates": n_dup,
                "n_nulls": n_nulls,
            })

    # =========================================================================
    # 4. API limits
    # =========================================================================
    api_limits = loader.api_limits + ex_data.api_limits
    if api_limits:
        print("\n" + "─" * 80)
        print("  API limits / errors observed")
        print("─" * 80)
        for note in api_limits:
            print(f"  - {note}")
    else:
        print("\n  [OK] No API limits / errors observed during smoke test")

    # =========================================================================
    # 5. missing_data_report
    # =========================================================================
    print("\n" + "─" * 80)
    print("  Phase 4 — Generate missing_data_report.md")
    print("─" * 80)

    # 把 ex_data 的 api_limits 也累進 loader 一起寫一份報告
    loader._api_limits.extend(ex_data.api_limits)
    report_path = loader.write_missing_data_report()
    if report_path:
        print(f"  [WARN] Report generated: {report_path}")
        print(f"\n  --- Report content (first 60 lines) ---")
        for line in report_path.read_text(encoding="utf-8").splitlines()[:60]:
            print(f"  {line}")
    else:
        print(f"  [OK] All data clean - no missing_data_report.md generated")

    # =========================================================================
    # Summary
    # =========================================================================
    print("\n" + "=" * 80)
    print("  Summary")
    print("=" * 80)
    print(f"  Total data sources fetched : {len(summary)}")
    n_clean = sum(1 for s in summary
                  if s.get("n_duplicates", 0) == 0
                  and s.get("n_nulls", 0) == 0
                  and s.get("is_sorted") in (True, None))
    print(f"  Sources clean              : {n_clean} / {len(summary)}")
    print(f"  API limits                 : {len(api_limits)}")
    print(f"  missing_data_report.md     : {'YES' if report_path else 'NO'}")
    print()


if __name__ == "__main__":
    main()

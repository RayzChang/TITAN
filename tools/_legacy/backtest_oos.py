"""
TITAN v1.3 — OOS（樣本外）回測驗證器

用法：
    .venv/Scripts/python.exe tools/backtest_oos.py

功能：
  - 取最近 365 天 BTC + ETH 的 1H/4H/1D 歷史
  - 切分 In-Sample (前 60%) 和 Out-of-Sample (後 40%)
  - 兩段都跑同一套策略
  - 並列輸出兩段結果，比較是否穩定
  - 若 IS 賺錢 OOS 大幅變差 → 策略不可靠
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from config.settings_loader import load_settings
from core.exchange import Exchange
from strategies.range_breakout import RangeBreakout
from backtest.data_loader import DataLoader
from backtest.engine_v13 import V13BacktestEngine


SYMBOLS = ['BTC/USDT:USDT', 'ETH/USDT:USDT']
TOTAL_DAYS = 365      # 拉一年資料
IS_RATIO   = 0.6      # 前 60% 為 In-Sample，後 40% 為 OOS


def main():
    print("=" * 70)
    print("  TITAN v1.3 OOS 回測驗證")
    print("=" * 70)

    cfg = load_settings()
    ex = Exchange(cfg)
    ex.connect()
    loader = DataLoader(ex)

    all_results = {}

    for symbol in SYMBOLS:
        print(f"\n{'─' * 70}")
        print(f"  {symbol}")
        print(f"{'─' * 70}")

        # 拉資料
        print(f"  → 拉取 {TOTAL_DAYS} 天歷史...")
        df_1h = loader.fetch(symbol, '1h', days=TOTAL_DAYS)
        df_4h = loader.fetch(symbol, '4h', days=TOTAL_DAYS)
        df_1d = loader.fetch(symbol, '1d', days=TOTAL_DAYS + 30)  # 多取一點給暖機

        print(f"  → 1H {len(df_1h)} 根 / 4H {len(df_4h)} 根 / 1D {len(df_1d)} 根")
        print(f"  → 期間 {df_1h.index[0]} ~ {df_1h.index[-1]}")

        # 切分 IS / OOS
        split_idx = int(len(df_1h) * IS_RATIO)
        split_time = df_1h.index[split_idx]
        print(f"  → 切分點：{split_time}")
        print(f"  → IS:  {df_1h.index[0]} ~ {split_time}")
        print(f"  → OOS: {split_time} ~ {df_1h.index[-1]}")

        # ── In-Sample 回測 ──
        is_df_1h = df_1h.iloc[:split_idx]
        is_df_4h = df_4h[df_4h.index < split_time]
        is_df_1d = df_1d[df_1d.index < split_time]

        strategy_is = RangeBreakout(cfg)  # 全新實例
        engine_is = V13BacktestEngine(strategy_is, cfg, symbol)
        try:
            is_result = engine_is.run(is_df_1h, is_df_4h, is_df_1d)
        except Exception as e:
            print(f"  ❌ IS 回測失敗：{e}")
            continue

        # ── Out-of-Sample 回測 ──
        oos_df_1h = df_1h.iloc[split_idx:]
        oos_df_4h = df_4h[df_4h.index >= split_time]
        oos_df_1d = df_1d[df_1d.index >= split_time]

        strategy_oos = RangeBreakout(cfg)  # 全新實例（無記憶）
        engine_oos = V13BacktestEngine(strategy_oos, cfg, symbol)
        try:
            oos_result = engine_oos.run(oos_df_1h, oos_df_4h, oos_df_1d)
        except Exception as e:
            print(f"  ❌ OOS 回測失敗：{e}")
            continue

        # ── 並排輸出 ──
        print()
        print(f"  ┌────────────────────┬─────────────┬─────────────┐")
        print(f"  │ {'指標':<18} │ {'In-Sample':>11} │ {'OOS':>11} │")
        print(f"  ├────────────────────┼─────────────┼─────────────┤")

        rows = [
            ('交易次數',     'total_trades',     '{}'),
            ('勝率',         'win_rate_pct',     '{:.2f}%'),
            ('總報酬',       'total_return_pct', '{:+.2f}%'),
            ('最終餘額',     'final_capital',    '${:,.2f}'),
            ('平均贏單',     'avg_win_usdt',     '+${:.2f}'),
            ('平均輸單',     'avg_loss_usdt',    '${:.2f}'),
            ('最大回撤',     'max_drawdown_pct', '{:.2f}%'),
            ('Profit Factor', 'profit_factor',   '{}'),
            ('Sharpe',       'sharpe_ratio',     '{:.2f}'),
        ]

        for label, key, fmt in rows:
            is_v  = is_result.get(key, 0)
            oos_v = oos_result.get(key, 0)
            try:
                is_str  = fmt.format(is_v)
                oos_str = fmt.format(oos_v)
            except (ValueError, TypeError):
                is_str  = str(is_v)
                oos_str = str(oos_v)
            print(f"  │ {label:<18} │ {is_str:>11} │ {oos_str:>11} │")

        print(f"  └────────────────────┴─────────────┴─────────────┘")

        # ── OOS 健康判斷 ──
        is_ret  = is_result.get('total_return_pct', 0)
        oos_ret = oos_result.get('total_return_pct', 0)
        verdict = _verdict(is_ret, oos_ret)
        print(f"\n  [評估]：{verdict}")

        all_results[symbol] = {'is': is_result, 'oos': oos_result}

    # 儲存完整結果
    out_path = Path('data/oos_results.json')
    out_path.parent.mkdir(parents=True, exist_ok=True)
    serializable = _make_serializable(all_results)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[儲存] 完整結果已存：{out_path}")


def _verdict(is_ret, oos_ret):
    if is_ret <= 0 and oos_ret <= 0:
        return "[X] 兩段都虧 - 策略本身有問題，不該跑"
    if is_ret > 0 and oos_ret <= 0:
        return "[!] IS 賺、OOS 虧 - 過擬合或運氣，不建議實盤"
    if is_ret <= 0 and oos_ret > 0:
        return "[!] IS 虧、OOS 賺 - 樣本不穩定，再多測幾段"
    ratio = oos_ret / is_ret if is_ret != 0 else 1
    if ratio > 0.5:
        return f"[OK] 兩段都賺，OOS 維持 IS 的 {ratio*100:.0f}% - 可信度高"
    else:
        return f"[!] 兩段都賺但 OOS 衰退到 IS 的 {ratio*100:.0f}% - 邊際偏弱"


def _make_serializable(obj):
    if isinstance(obj, dict):
        return {k: _make_serializable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_make_serializable(v) for v in obj]
    if isinstance(obj, (pd.Timestamp, datetime)):
        return str(obj)
    return obj


if __name__ == "__main__":
    main()

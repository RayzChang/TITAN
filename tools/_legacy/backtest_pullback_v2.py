"""
TITAN — TrendPullback V1 vs V2 vs 朋友箱體 對決
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
from pathlib import Path

import pandas as pd

from config.settings_loader import load_settings
from core.exchange import Exchange
from strategies.range_breakout import RangeBreakout
from strategies.candidates import TrendPullback, TrendPullbackV2
from backtest.data_loader import DataLoader
from backtest.engine_v13 import V13BacktestEngine
from backtest.engine_simple import SimpleBacktestEngine

SYMBOLS = ['BTC/USDT:USDT', 'ETH/USDT:USDT']
DAYS = 365
IS_RATIO = 0.6


def fmt(v, f):
    try:    return f.format(v)
    except: return str(v)


def main():
    print("=" * 78)
    print("  TrendPullback V1 vs V2 vs 朋友箱體")
    print("=" * 78)

    cfg = load_settings()
    ex = Exchange(cfg); ex.connect()
    loader = DataLoader(ex)

    all_results = {}

    for symbol in SYMBOLS:
        print(f"\n{'#' * 78}\n  {symbol}\n{'#' * 78}")

        df_1h = loader.fetch(symbol, '1h', days=DAYS)
        df_4h = loader.fetch(symbol, '4h', days=DAYS)
        df_1d = loader.fetch(symbol, '1d', days=DAYS + 30)

        split_idx = int(len(df_1h) * IS_RATIO)
        split_t = df_1h.index[split_idx]

        is_1h, oos_1h = df_1h.iloc[:split_idx], df_1h.iloc[split_idx:]
        is_4h, oos_4h = df_4h[df_4h.index < split_t], df_4h[df_4h.index >= split_t]
        is_1d, oos_1d = df_1d[df_1d.index < split_t], df_1d[df_1d.index >= split_t]

        results = {}

        # 朋友箱體
        print(f"  [1/3] 朋友箱體...")
        try:
            results['box_is']  = V13BacktestEngine(RangeBreakout(cfg), cfg, symbol).run(is_1h, is_4h, is_1d)
            results['box_oos'] = V13BacktestEngine(RangeBreakout(cfg), cfg, symbol).run(oos_1h, oos_4h, oos_1d)
        except Exception as e:
            print(f"   ERR: {e}")

        # V1
        print(f"  [2/3] TrendPullback V1...")
        results['v1_is']  = SimpleBacktestEngine(TrendPullback(cfg), cfg, symbol).run(is_1h, is_4h, is_1d)
        results['v1_oos'] = SimpleBacktestEngine(TrendPullback(cfg), cfg, symbol).run(oos_1h, oos_4h, oos_1d)

        # V2
        print(f"  [3/3] TrendPullback V2...")
        results['v2_is']  = SimpleBacktestEngine(TrendPullbackV2(cfg), cfg, symbol).run(is_1h, is_4h, is_1d)
        results['v2_oos'] = SimpleBacktestEngine(TrendPullbackV2(cfg), cfg, symbol).run(oos_1h, oos_4h, oos_1d)

        # 表格
        print()
        print(f"  {'指標':<13} | {'朋友箱體':<28} | {'回調 V1':<28} | {'回調 V2':<28}")
        print(f"  {'-'*13} | {'-'*28} | {'-'*28} | {'-'*28}")
        for label, key, f in [
            ('交易數',     'total_trades',     '{}'),
            ('勝率',       'win_rate_pct',     '{:.1f}%'),
            ('總報酬',     'total_return_pct', '{:+.2f}%'),
            ('餘額',       'final_capital',    '${:,.0f}'),
            ('平均贏',     'avg_win_usdt',     '+${:.0f}'),
            ('平均輸',     'avg_loss_usdt',    '${:.2f}'),
            ('最大回撤',   'max_drawdown_pct', '{:.1f}%'),
            ('PF',         'profit_factor',    '{}'),
            ('Sharpe',     'sharpe_ratio',     '{:.2f}'),
        ]:
            cells = []
            for sname in ['box', 'v1', 'v2']:
                is_v  = results[f'{sname}_is'].get(key, 0)
                oos_v = results[f'{sname}_oos'].get(key, 0)
                cells.append(f"IS:{fmt(is_v, f)}  OOS:{fmt(oos_v, f)}")
            print(f"  {label:<13} | {cells[0]:<28} | {cells[1]:<28} | {cells[2]:<28}")

        # 評選
        print()
        print(f"  --- 綜合評估 (OOS：報酬+Sharpe×5-DD) ---")
        scores = {}
        for sname in ['box', 'v1', 'v2']:
            o = results[f'{sname}_oos']
            s = (float(o.get('total_return_pct',0))
                 + float(o.get('sharpe_ratio',0))*5
                 - float(o.get('max_drawdown_pct',0)))
            scores[sname] = round(s, 2)
            print(f"    {sname:<8}: 報酬{o.get('total_return_pct',0):+6.2f}% Sharpe{o.get('sharpe_ratio',0):+5.2f} "
                  f"DD{o.get('max_drawdown_pct',0):5.1f}% 次{o.get('total_trades',0):3d} → {scores[sname]}")
        winner = max(scores, key=scores.get)
        print(f"\n  >>> 冠軍：{winner.upper()}")

        all_results[symbol] = results

    # 儲存
    out = Path('data/pullback_v2_results.json')
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        def s(o):
            if isinstance(o, dict): return {k: s(v) for k, v in o.items()}
            if isinstance(o, list): return [s(v) for v in o]
            if hasattr(o, 'isoformat'): return str(o)
            return o
        json.dump(s(all_results), f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[儲存] {out}")


if __name__ == "__main__":
    main()

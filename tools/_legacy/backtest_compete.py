"""
TITAN — 三策略同場競技

對比：
  1. 朋友箱體（RangeBreakout v1.2）
  2. MIA 候選 A：TrendPullback
  3. MIA 候選 B：VolumeMomentum

各自跑 IS + OOS，並列輸出。
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
from pathlib import Path

import pandas as pd

from config.settings_loader import load_settings
from core.exchange import Exchange
from strategies.range_breakout import RangeBreakout
from strategies.candidates import TrendPullback, VolumeMomentum
from backtest.data_loader import DataLoader
from backtest.engine_v13 import V13BacktestEngine
from backtest.engine_simple import SimpleBacktestEngine


SYMBOLS = ['BTC/USDT:USDT', 'ETH/USDT:USDT']
TOTAL_DAYS = 365
IS_RATIO   = 0.6


def run_strategy(name, strategy, engine_cls, cfg, symbol,
                 df_1h, df_4h, df_1d):
    """跑一個策略的 IS + OOS"""
    split_idx  = int(len(df_1h) * IS_RATIO)
    split_time = df_1h.index[split_idx]

    is_1h  = df_1h.iloc[:split_idx]
    is_4h  = df_4h[df_4h.index < split_time]
    is_1d  = df_1d[df_1d.index < split_time]
    oos_1h = df_1h.iloc[split_idx:]
    oos_4h = df_4h[df_4h.index >= split_time]
    oos_1d = df_1d[df_1d.index >= split_time]

    # IS
    if engine_cls == V13BacktestEngine:
        eng_is = engine_cls(strategy.__class__(cfg), cfg, symbol)
        is_res = eng_is.run(is_1h, is_4h, is_1d)
        eng_oos = engine_cls(strategy.__class__(cfg), cfg, symbol)
        oos_res = eng_oos.run(oos_1h, oos_4h, oos_1d)
    else:
        eng_is = engine_cls(strategy.__class__(cfg), cfg, symbol)
        is_res = eng_is.run(is_1h, is_4h, is_1d)
        eng_oos = engine_cls(strategy.__class__(cfg), cfg, symbol)
        oos_res = eng_oos.run(oos_1h, oos_4h, oos_1d)

    return is_res, oos_res


def fmt(v, fmt_str):
    try:
        return fmt_str.format(v)
    except (ValueError, TypeError):
        return str(v)


def print_table(symbol, results):
    """並列輸出三策略結果"""
    print()
    print(f"  {'指標':<16} | {'A: 朋友箱體':<22} | {'B: 趨勢回調':<22} | {'C: 爆量動能':<22}")
    print(f"  {'-'*16} | {'-'*22} | {'-'*22} | {'-'*22}")
    rows = [
        ('交易數',       'total_trades',     '{}'),
        ('勝率',         'win_rate_pct',     '{:.1f}%'),
        ('總報酬',       'total_return_pct', '{:+.2f}%'),
        ('最終餘額',     'final_capital',    '${:,.0f}'),
        ('平均贏單',     'avg_win_usdt',     '+${:.0f}'),
        ('平均輸單',     'avg_loss_usdt',    '${:.2f}'),
        ('最大回撤',     'max_drawdown_pct', '{:.1f}%'),
        ('Profit Factor','profit_factor',    '{}'),
        ('Sharpe',       'sharpe_ratio',     '{:.2f}'),
    ]
    for label, key, f in rows:
        cells = []
        for strat_name in ['box', 'pullback', 'momentum']:
            is_v  = results[strat_name]['is'].get(key, 0)
            oos_v = results[strat_name]['oos'].get(key, 0)
            cells.append(f"IS:{fmt(is_v, f)} OOS:{fmt(oos_v, f)}")
        print(f"  {label:<16} | {cells[0]:<22} | {cells[1]:<22} | {cells[2]:<22}")


def winner_analysis(symbol, results):
    """挑出最佳"""
    print()
    print(f"  --- {symbol} 評選 ---")
    scores = {}
    for name in ['box', 'pullback', 'momentum']:
        oos = results[name]['oos']
        # 加權分：總報酬 + Sharpe×5 - 最大回撤
        score = (
            float(oos.get('total_return_pct', 0))
            + float(oos.get('sharpe_ratio', 0)) * 5
            - float(oos.get('max_drawdown_pct', 0))
        )
        scores[name] = round(score, 2)
        print(f"    {name:<10}: OOS報酬 {oos.get('total_return_pct',0):+.2f}% / "
              f"Sharpe {oos.get('sharpe_ratio',0):.2f} / "
              f"DD {oos.get('max_drawdown_pct',0):.1f}% / "
              f"次數 {oos.get('total_trades',0)} → 綜合分 {scores[name]}")
    winner = max(scores, key=scores.get)
    print(f"  >>> 冠軍：{winner.upper()}")


def main():
    print("=" * 72)
    print("  TITAN — 三策略同場競技（IS vs OOS）")
    print("=" * 72)

    cfg = load_settings()
    ex = Exchange(cfg); ex.connect()
    loader = DataLoader(ex)

    all_results = {}

    for symbol in SYMBOLS:
        print(f"\n{'#' * 72}")
        print(f"  {symbol}")
        print(f"{'#' * 72}")

        df_1h = loader.fetch(symbol, '1h', days=TOTAL_DAYS)
        df_4h = loader.fetch(symbol, '4h', days=TOTAL_DAYS)
        df_1d = loader.fetch(symbol, '1d', days=TOTAL_DAYS + 30)

        results = {}

        # 1. 朋友箱體
        print(f"\n  [1/3] 跑朋友箱體 (RangeBreakout v1.2)...")
        try:
            is_r, oos_r = run_strategy(
                'box', RangeBreakout(cfg), V13BacktestEngine, cfg, symbol,
                df_1h, df_4h, df_1d,
            )
            results['box'] = {'is': is_r, 'oos': oos_r}
        except Exception as e:
            print(f"    ERROR: {e}")
            results['box'] = {'is': {}, 'oos': {}}

        # 2. 趨勢回調
        print(f"  [2/3] 跑 MIA 候選 A (TrendPullback)...")
        try:
            is_r, oos_r = run_strategy(
                'pullback', TrendPullback(cfg), SimpleBacktestEngine, cfg, symbol,
                df_1h, df_4h, df_1d,
            )
            results['pullback'] = {'is': is_r, 'oos': oos_r}
        except Exception as e:
            print(f"    ERROR: {e}")
            results['pullback'] = {'is': {}, 'oos': {}}

        # 3. 爆量動能
        print(f"  [3/3] 跑 MIA 候選 B (VolumeMomentum)...")
        try:
            is_r, oos_r = run_strategy(
                'momentum', VolumeMomentum(cfg), SimpleBacktestEngine, cfg, symbol,
                df_1h, df_4h, df_1d,
            )
            results['momentum'] = {'is': is_r, 'oos': oos_r}
        except Exception as e:
            print(f"    ERROR: {e}")
            results['momentum'] = {'is': {}, 'oos': {}}

        print_table(symbol, results)
        winner_analysis(symbol, results)
        all_results[symbol] = results

    # 儲存
    out = Path('data/compete_results.json')
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(_serialize(all_results), f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[儲存] {out}")


def _serialize(o):
    if isinstance(o, dict): return {k: _serialize(v) for k, v in o.items()}
    if isinstance(o, list): return [_serialize(v) for v in o]
    if hasattr(o, 'isoformat'): return str(o)
    return o


if __name__ == "__main__":
    main()

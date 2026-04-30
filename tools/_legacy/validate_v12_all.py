"""
TITAN — V1 vs V1.2A vs V1.2B vs V1.2C 完整驗證對決
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import random
from pathlib import Path

import numpy as np
import pandas as pd

from config.settings_loader import load_settings
from core.exchange import Exchange
from strategies.candidates import (
    TrendPullback, TrendPullbackV12A, TrendPullbackV12B, TrendPullbackV12C,
)
from backtest.data_loader import DataLoader
from backtest.engine_simple import SimpleBacktestEngine

SYMBOLS = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'DOGE/USDT:USDT', 'ATOM/USDT:USDT']
DAYS = 365
IS_RATIO = 0.6
MCPT_RUNS = 1000
N_FOLDS = 5
TAKER_FEE = 0.0005

STRATS = [
    ('V1',    TrendPullback),
    ('V1.2A', TrendPullbackV12A),
    ('V1.2B', TrendPullbackV12B),
    ('V1.2C', TrendPullbackV12C),
]


def run_strategy(strategy_cls, df_1h, df_4h, df_1d, cfg, symbol):
    eng = SimpleBacktestEngine(strategy_cls(cfg), cfg, symbol)
    return eng.run(df_1h, df_4h, df_1d)


def simulate_random(df_1h, n_trades, position_usdt, leverage):
    if len(df_1h) < 50 or n_trades == 0:
        return 0.0
    indices = random.sample(range(50, len(df_1h) - 10), min(n_trades, len(df_1h) - 60))
    total = 0.0
    for i in indices:
        entry = float(df_1h.iloc[i]['open'])
        side = random.choice(['LONG', 'SHORT'])
        sl_pct, tp_pct = 0.01, 0.02
        if side == 'LONG':
            sl, tp = entry * (1 - sl_pct), entry * (1 + tp_pct)
        else:
            sl, tp = entry * (1 + sl_pct), entry * (1 - tp_pct)
        exit_price = entry
        for j in range(i + 1, min(i + 11, len(df_1h))):
            bar = df_1h.iloc[j]
            if side == 'LONG':
                if bar['low'] <= sl:  exit_price = sl; break
                if bar['high'] >= tp: exit_price = tp; break
            else:
                if bar['high'] >= sl: exit_price = sl; break
                if bar['low'] <= tp:  exit_price = tp; break
        else:
            exit_price = float(df_1h.iloc[min(i + 10, len(df_1h) - 1)]['close'])
        raw = (exit_price - entry) / entry if side == 'LONG' else (entry - exit_price) / entry
        net = raw * leverage - TAKER_FEE * 2
        total += position_usdt * net
    return total


def mcpt_one(strategy_cls, symbol, df_1h, df_4h, df_1d, cfg):
    split_idx = int(len(df_1h) * IS_RATIO)
    split_t = df_1h.index[split_idx]
    oos_1h = df_1h.iloc[split_idx:]
    oos_4h = df_4h[df_4h.index >= split_t]
    oos_1d = df_1d[df_1d.index >= split_t] if df_1d is not None else None

    r = run_strategy(strategy_cls, oos_1h, oos_4h, oos_1d, cfg, symbol)
    n_trades = r['total_trades']
    real_pnl = sum(t['pnl_usdt'] for t in r['trades'])

    if n_trades == 0:
        return {'n_trades': 0, 'real_pnl': 0, 'real_ret_pct': 0, 'real_dd': 0,
                'p_value': None, 'verdict': '無交易'}

    pos_usdt = float(cfg.get('capital', {}).get('position_fixed_usdt', 100))
    lev = int(cfg.get('risk', {}).get('leverage', 100))

    random.seed(42)
    rand_pnls = np.array([simulate_random(oos_1h, n_trades, pos_usdt, lev)
                          for _ in range(MCPT_RUNS)])
    p_value = float((rand_pnls >= real_pnl).sum() / MCPT_RUNS)
    verdict = '通過' if p_value < 0.05 else ('邊緣' if p_value < 0.20 else '不通過')

    return {
        'n_trades': n_trades, 'real_pnl': real_pnl,
        'real_ret_pct': r['total_return_pct'],
        'real_dd': r['max_drawdown_pct'],
        'p_value': p_value, 'verdict': verdict,
    }


def walkforward_one(strategy_cls, symbol, df_1h, df_4h, df_1d, cfg):
    n = len(df_1h)
    fold_size = n // N_FOLDS
    folds = []
    for k in range(N_FOLDS):
        start = k * fold_size
        end = (k + 1) * fold_size if k < N_FOLDS - 1 else n
        seg_1h = df_1h.iloc[start:end]
        if len(seg_1h) < 100:
            continue
        seg_start_t, seg_end_t = seg_1h.index[0], seg_1h.index[-1]
        seg_4h = df_4h[(df_4h.index >= seg_start_t) & (df_4h.index <= seg_end_t)]
        seg_1d = (df_1d[(df_1d.index >= seg_start_t) & (df_1d.index <= seg_end_t)]
                  if df_1d is not None else None)
        try:
            r = run_strategy(strategy_cls, seg_1h, seg_4h, seg_1d, cfg, symbol)
            folds.append({
                'fold': k + 1,
                'trades': r['total_trades'],
                'return_pct': r['total_return_pct'],
            })
        except Exception:
            pass
    rets = [f['return_pct'] for f in folds]
    return {
        'folds': folds,
        'positive': sum(1 for r in rets if r > 0),
        'total': len(folds),
        'avg_ret': sum(rets) / len(rets) if rets else 0,
    }


def main():
    print("=" * 90)
    print("  TITAN — V1 vs V1.2A vs V1.2B vs V1.2C 三方案比拼")
    print("=" * 90)

    cfg = load_settings()
    ex = Exchange(cfg); ex.connect()
    loader = DataLoader(ex)

    all_results = {}

    for symbol in SYMBOLS:
        coin = symbol.split('/')[0]
        print(f"\n{'─' * 90}\n  {coin}\n{'─' * 90}")

        df_1h = loader.fetch(symbol, '1h', days=DAYS)
        df_4h = loader.fetch(symbol, '4h', days=DAYS)
        df_1d = loader.fetch(symbol, '1d', days=DAYS + 60)

        sym_results = {}

        for strat_name, strat_cls in STRATS:
            print(f"  [{strat_name}] ", end='', flush=True)
            mcpt = mcpt_one(strat_cls, symbol, df_1h, df_4h, df_1d, cfg)
            wf = walkforward_one(strat_cls, symbol, df_1h, df_4h, df_1d, cfg)

            wf_str = ' '.join([f"{f['return_pct']:+5.1f}%" for f in wf['folds']])
            print(
                f"trades={mcpt.get('n_trades',0):3d} pnl={mcpt.get('real_pnl',0):+6.0f} "
                f"DD={mcpt.get('real_dd',0):4.1f}% p={mcpt.get('p_value','-'):>6} "
                f"({mcpt.get('verdict','-')}) | WF: {wf_str} 賺{wf['positive']}/{wf['total']}"
            )
            sym_results[strat_name] = {'mcpt': mcpt, 'wf': wf}

        all_results[symbol] = sym_results

    # 總結對照表
    print()
    print("=" * 90)
    print("  總結對照（按綜合分排序：MCPT 通過 ×30 + WF 賺錢段 ×10 + 報酬）")
    print("=" * 90)
    print()
    print(f"  {'策略':<6} | {'MCPT 通過':<10} | {'WF 賺錢段':<10} | {'平均 OOS 報酬':<12} | {'平均回撤':<10}")
    print(f"  {'-'*6} | {'-'*10} | {'-'*10} | {'-'*12} | {'-'*10}")

    summary = {}
    for strat_name, _ in STRATS:
        mcpt_pass = sum(1 for sym in all_results
                        if all_results[sym][strat_name]['mcpt'].get('p_value') is not None
                        and all_results[sym][strat_name]['mcpt']['p_value'] < 0.05)
        wf_total_pos  = sum(all_results[sym][strat_name]['wf']['positive'] for sym in all_results)
        wf_total_seg  = sum(all_results[sym][strat_name]['wf']['total'] for sym in all_results)
        avg_ret = np.mean([all_results[sym][strat_name]['mcpt'].get('real_ret_pct', 0)
                           for sym in all_results])
        avg_dd  = np.mean([all_results[sym][strat_name]['mcpt'].get('real_dd', 0)
                           for sym in all_results])

        score = mcpt_pass * 30 + wf_total_pos * 10 + avg_ret - avg_dd
        summary[strat_name] = {
            'mcpt_pass': f"{mcpt_pass}/4",
            'wf_pos': f"{wf_total_pos}/{wf_total_seg}",
            'avg_ret': f"{avg_ret:+.2f}%",
            'avg_dd': f"{avg_dd:.1f}%",
            'score': score,
        }

    sorted_strats = sorted(summary.items(), key=lambda x: x[1]['score'], reverse=True)
    for strat_name, s in sorted_strats:
        print(f"  {strat_name:<6} | {s['mcpt_pass']:<10} | {s['wf_pos']:<10} | {s['avg_ret']:<12} | {s['avg_dd']:<10} score={s['score']:.0f}")

    # 冠軍
    winner_name = sorted_strats[0][0]
    print(f"\n  >>> 綜合冠軍：{winner_name}")

    out = Path('data/v12_all_validation.json')
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[儲存] {out}")


if __name__ == "__main__":
    main()

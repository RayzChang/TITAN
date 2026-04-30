"""
TITAN — V1.1 完整驗證（MCPT + Walk-Forward）並跟 V1 對比
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
from strategies.candidates import TrendPullback, TrendPullbackV11
from backtest.data_loader import DataLoader
from backtest.engine_simple import SimpleBacktestEngine

SYMBOLS = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'DOGE/USDT:USDT', 'ATOM/USDT:USDT']
DAYS = 365
IS_RATIO = 0.6
MCPT_RUNS = 1000
N_FOLDS = 5
TAKER_FEE = 0.0005


def run_strategy(strategy_cls, df_1h, df_4h, cfg, symbol):
    eng = SimpleBacktestEngine(strategy_cls(cfg), cfg, symbol)
    return eng.run(df_1h, df_4h, None)


def simulate_random(df_1h, n_trades, position_usdt, leverage, seed=None):
    if seed is not None:
        random.seed(seed)
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


def mcpt_one(strategy_cls, symbol, df_1h, df_4h, cfg):
    """跑單個策略的 MCPT"""
    split_idx = int(len(df_1h) * IS_RATIO)
    split_t = df_1h.index[split_idx]
    oos_1h = df_1h.iloc[split_idx:]
    oos_4h = df_4h[df_4h.index >= split_t]

    r = run_strategy(strategy_cls, oos_1h, oos_4h, cfg, symbol)
    n_trades = r['total_trades']
    real_pnl = sum(t['pnl_usdt'] for t in r['trades'])

    if n_trades == 0:
        return {'n_trades': 0, 'real_pnl': 0, 'p_value': None, 'verdict': '無交易'}

    pos_usdt = float(cfg.get('capital', {}).get('position_fixed_usdt', 100))
    lev = int(cfg.get('risk', {}).get('leverage', 100))

    random.seed(42)
    rand_pnls = [simulate_random(oos_1h, n_trades, pos_usdt, lev) for _ in range(MCPT_RUNS)]
    rand_pnls = np.array(rand_pnls)
    p_value = float((rand_pnls >= real_pnl).sum() / MCPT_RUNS)

    if p_value < 0.05:
        verdict = '通過'
    elif p_value < 0.20:
        verdict = '邊緣'
    else:
        verdict = '不通過'

    return {
        'n_trades':      n_trades,
        'real_pnl':      real_pnl,
        'real_ret_pct':  r['total_return_pct'],
        'real_sharpe':   r['sharpe_ratio'],
        'random_mean':   float(rand_pnls.mean()),
        'random_max':    float(rand_pnls.max()),
        'p_value':       p_value,
        'verdict':       verdict,
    }


def walkforward_one(strategy_cls, symbol, df_1h, df_4h, cfg):
    """跑單個策略的 walk-forward"""
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
        try:
            r = run_strategy(strategy_cls, seg_1h, seg_4h, cfg, symbol)
            folds.append({
                'fold': k + 1,
                'trades': r['total_trades'],
                'return_pct': r['total_return_pct'],
                'sharpe':     r['sharpe_ratio'],
            })
        except Exception:
            pass

    rets = [f['return_pct'] for f in folds]
    pos = sum(1 for r in rets if r > 0)
    return {
        'folds':    folds,
        'positive': pos,
        'total':    len(folds),
        'avg_ret':  sum(rets) / len(rets) if rets else 0,
    }


def main():
    print("=" * 78)
    print("  TITAN V1.1 (含 ADX Regime Filter) 完整驗證")
    print("=" * 78)

    cfg = load_settings()
    ex = Exchange(cfg); ex.connect()
    loader = DataLoader(ex)

    all_results = {}

    for symbol in SYMBOLS:
        print(f"\n{'─' * 78}\n  {symbol}\n{'─' * 78}")
        df_1h = loader.fetch(symbol, '1h', days=DAYS)
        df_4h = loader.fetch(symbol, '4h', days=DAYS)

        # MCPT V1
        print(f"  [V1   MCPT]  ", end='', flush=True)
        v1_mcpt = mcpt_one(TrendPullback, symbol, df_1h, df_4h, cfg)
        print(f"trades={v1_mcpt.get('n_trades',0)} pnl={v1_mcpt.get('real_pnl',0):+.0f} p={v1_mcpt.get('p_value','-')} -> {v1_mcpt.get('verdict','-')}")

        # MCPT V1.1
        print(f"  [V1.1 MCPT]  ", end='', flush=True)
        v11_mcpt = mcpt_one(TrendPullbackV11, symbol, df_1h, df_4h, cfg)
        print(f"trades={v11_mcpt.get('n_trades',0)} pnl={v11_mcpt.get('real_pnl',0):+.0f} p={v11_mcpt.get('p_value','-')} -> {v11_mcpt.get('verdict','-')}")

        # WF V1
        v1_wf  = walkforward_one(TrendPullback, symbol, df_1h, df_4h, cfg)
        v1_wf_str = ' '.join([f"{f['return_pct']:+5.1f}%" for f in v1_wf['folds']])
        print(f"  [V1   WF]    {v1_wf_str} | 賺{v1_wf['positive']}/{v1_wf['total']} 平均{v1_wf['avg_ret']:+.1f}%")

        # WF V1.1
        v11_wf = walkforward_one(TrendPullbackV11, symbol, df_1h, df_4h, cfg)
        v11_wf_str = ' '.join([f"{f['return_pct']:+5.1f}%" for f in v11_wf['folds']])
        print(f"  [V1.1 WF]    {v11_wf_str} | 賺{v11_wf['positive']}/{v11_wf['total']} 平均{v11_wf['avg_ret']:+.1f}%")

        all_results[symbol] = {
            'v1_mcpt':  v1_mcpt,  'v11_mcpt': v11_mcpt,
            'v1_wf':    v1_wf,    'v11_wf':   v11_wf,
        }

    # 總表
    print()
    print("=" * 78)
    print("  總結對比 (V1 vs V1.1)")
    print("=" * 78)
    print()
    print(f"  {'幣':<6} | {'MCPT V1':<14} | {'MCPT V1.1':<14} | {'WF V1':<14} | {'WF V1.1':<14}")
    print(f"  {'-'*6} | {'-'*14} | {'-'*14} | {'-'*14} | {'-'*14}")
    for sym, r in all_results.items():
        coin = sym.split('/')[0]
        v1m  = r['v1_mcpt']
        v11m = r['v11_mcpt']
        v1w  = r['v1_wf']
        v11w = r['v11_wf']

        v1m_str  = f"p={v1m.get('p_value','-')}/{v1m.get('verdict','-')}" if v1m.get('p_value') is not None else '無交易'
        v11m_str = f"p={v11m.get('p_value','-')}/{v11m.get('verdict','-')}" if v11m.get('p_value') is not None else '無交易'
        v1w_str  = f"{v1w['positive']}/{v1w['total']} avg{v1w['avg_ret']:+.0f}%"
        v11w_str = f"{v11w['positive']}/{v11w['total']} avg{v11w['avg_ret']:+.0f}%"

        print(f"  {coin:<6} | {v1m_str:<14} | {v11m_str:<14} | {v1w_str:<14} | {v11w_str:<14}")

    # 整體結論
    print()
    v11_pass = sum(1 for r in all_results.values()
                   if r['v11_mcpt'].get('p_value') is not None
                   and r['v11_mcpt']['p_value'] < 0.05)
    v11_wf_stable = sum(1 for r in all_results.values()
                        if r['v11_wf']['positive'] >= 4)
    v11_wf_ok = sum(1 for r in all_results.values()
                    if r['v11_wf']['positive'] >= 3)

    print("=" * 78)
    print(f"  V1.1 MCPT 顯著通過: {v11_pass}/{len(all_results)}")
    print(f"  V1.1 WF 穩定 (4/5+): {v11_wf_stable}/{len(all_results)}")
    print(f"  V1.1 WF 尚可 (3/5+): {v11_wf_ok}/{len(all_results)}")
    print("=" * 78)

    out = Path('data/v11_validation.json')
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[儲存] {out}")


if __name__ == "__main__":
    main()

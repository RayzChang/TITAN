"""
Portfolio-level walk-forward validation for TITAN V2 candidate alphas.
"""

import json
import os
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.data_loader import DataLoader
from backtest.engine_portfolio_v2 import PortfolioEngineV2
from config.settings_loader import load_settings
from core.exchange import Exchange
from strategies.candidates import (
    Fakeout2B,
    RegimeMomentumBreakout,
    Reversal123,
    TrendPullback,
    TrendPullbackV2_SL075,
    TrendPullbackV2_SL100,
)


TOP20 = [
    'BTC/USDT:USDT', 'ETH/USDT:USDT', 'BNB/USDT:USDT', 'SOL/USDT:USDT',
    'XRP/USDT:USDT', 'ADA/USDT:USDT', 'DOGE/USDT:USDT', 'AVAX/USDT:USDT',
    'TRX/USDT:USDT', 'LINK/USDT:USDT', 'DOT/USDT:USDT', 'LTC/USDT:USDT',
    'BCH/USDT:USDT', 'NEAR/USDT:USDT', 'ATOM/USDT:USDT', 'FIL/USDT:USDT',
    'APT/USDT:USDT', 'ARB/USDT:USDT', 'OP/USDT:USDT', 'SUI/USDT:USDT',
]
ACTIVE_VALIDATED = ['BTC/USDT:USDT', 'DOGE/USDT:USDT', 'ATOM/USDT:USDT']
TOTAL_DAYS = 365
N_FOLDS = 5
WARMBACK_DAYS = 35


def fetch_all(loader):
    data = {}
    for sym in TOP20:
        try:
            data[sym] = {
                'df_1h': loader.fetch(sym, '1h', days=TOTAL_DAYS),
                'df_4h': loader.fetch(sym, '4h', days=TOTAL_DAYS + WARMBACK_DAYS),
                'df_1d': loader.fetch(sym, '1d', days=TOTAL_DAYS + WARMBACK_DAYS),
            }
        except Exception as exc:
            print(f"[WARN] skip {sym}: {exc}")
    return data


def shadow_for(active):
    return [s for s in TOP20 if s not in active]


def slice_fold(data, start_idx, end_idx, ref_index):
    start_t = ref_index[start_idx]
    end_t = ref_index[end_idx - 1]
    warm_t = start_t - pd.Timedelta(days=WARMBACK_DAYS)
    out = {}
    for sym, d in data.items():
        df_1h = d['df_1h']
        seg_1h = df_1h[(df_1h.index >= start_t) & (df_1h.index <= end_t)]
        if len(seg_1h) < 100:
            continue
        out[sym] = {
            'df_1h': seg_1h,
            'df_4h': d['df_4h'][(d['df_4h'].index >= warm_t) & (d['df_4h'].index <= end_t)],
            'df_1d': d['df_1d'][(d['df_1d'].index >= warm_t) & (d['df_1d'].index <= end_t)],
        }
    return out, start_t, end_t


def run_engine(strategy_cls, cfg, data, active):
    eng = PortfolioEngineV2(
        strategy_factory=lambda c=strategy_cls: c(cfg),
        settings=cfg,
        active_list=active,
        shadow_list=shadow_for(active),
    )
    return eng.run(data)


def main():
    cfg = load_settings()
    ex = Exchange(cfg)
    ex.connect()
    loader = DataLoader(ex)

    data = fetch_all(loader)
    if not data:
        raise RuntimeError('no data loaded')

    ref_symbol = 'BTC/USDT:USDT' if 'BTC/USDT:USDT' in data else next(iter(data))
    ref_index = data[ref_symbol]['df_1h'].index
    fold_size = len(ref_index) // N_FOLDS

    strategies = [
        ('V1-ACTIVE3', TrendPullback, ACTIVE_VALIDATED),
        ('V2-SL-075-ACT3', TrendPullbackV2_SL075, ACTIVE_VALIDATED),
        ('V2-SL-100-ACT3', TrendPullbackV2_SL100, ACTIVE_VALIDATED),
        ('123-ALL20', Reversal123, TOP20),
        ('2B-ALL20', Fakeout2B, TOP20),
        ('V3-REGIME-ALL20', RegimeMomentumBreakout, TOP20),
    ]

    results = {}
    for name, cls, active in strategies:
        print(f"\n=== {name} ===")
        rows = []
        for k in range(N_FOLDS):
            start = k * fold_size
            end = (k + 1) * fold_size if k < N_FOLDS - 1 else len(ref_index)
            fold_data, start_t, end_t = slice_fold(data, start, end, ref_index)
            if not fold_data:
                continue
            r = run_engine(cls, cfg, fold_data, active)
            active_m = r['active']
            row = {
                'fold': k + 1,
                'start': str(start_t),
                'end': str(end_t),
                'trades': active_m['total_trades'],
                'return_pct': active_m['total_return_pct'],
                'pnl_usdt': active_m['total_pnl_usdt'],
                'win_rate_pct': active_m['win_rate_pct'],
                'max_drawdown_pct': r['max_drawdown_pct'],
                'sharpe_ratio': active_m['sharpe_ratio'],
                'fully_stopped': r['fully_stopped'],
            }
            rows.append(row)
            print(
                f"fold {k + 1}: trades={row['trades']:3d} "
                f"ret={row['return_pct']:+7.2f}% dd={row['max_drawdown_pct']:5.2f}% "
                f"stopped={row['fully_stopped']}"
            )
        positive = sum(1 for row in rows if row['return_pct'] > 0)
        results[name] = {
            'folds': rows,
            'positive_folds': positive,
            'n_folds': len(rows),
        }
        print(f"positive folds: {positive}/{len(rows)}")

    out = Path('data/walkforward_v2.json')
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(f"\n[written] {out}")


if __name__ == '__main__':
    main()

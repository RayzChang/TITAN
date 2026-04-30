"""
TITAN V2 — Aggressive Passive 完整驗證
依 RICK V2 規格實作。
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
from pathlib import Path

import pandas as pd

from config.settings_loader import load_settings
from core.exchange import Exchange
from strategies.candidates import (
    TrendPullback,
    TrendPullbackV2_SL075, TrendPullbackV2_SL100, TrendPullbackV2_DYN,
    Reversal123, Fakeout2B, RegimeMomentumBreakout,
)
from backtest.data_loader import DataLoader
from backtest.engine_portfolio_v2 import PortfolioEngineV2

TOP20 = [
    'BTC/USDT:USDT', 'ETH/USDT:USDT', 'BNB/USDT:USDT', 'SOL/USDT:USDT',
    'XRP/USDT:USDT', 'ADA/USDT:USDT', 'DOGE/USDT:USDT', 'AVAX/USDT:USDT',
    'TRX/USDT:USDT', 'LINK/USDT:USDT', 'DOT/USDT:USDT', 'LTC/USDT:USDT',
    'BCH/USDT:USDT', 'NEAR/USDT:USDT', 'ATOM/USDT:USDT', 'FIL/USDT:USDT',
    'APT/USDT:USDT',  'ARB/USDT:USDT',  'OP/USDT:USDT',  'SUI/USDT:USDT',
]
ACTIVE_VALIDATED = ['BTC/USDT:USDT', 'DOGE/USDT:USDT', 'ATOM/USDT:USDT']  # legacy MCPT candidates
DAYS = 365
IS_RATIO = 0.6


def fetch_all(loader):
    data = {}
    for sym in TOP20:
        try:
            data[sym] = {
                'df_1h': loader.fetch(sym, '1h', days=DAYS),
                'df_4h': loader.fetch(sym, '4h', days=DAYS),
                'df_1d': loader.fetch(sym, '1d', days=DAYS + 60),
            }
        except Exception:
            pass
    return data


def split_oos(data):
    is_d, oos_d = {}, {}
    for sym, d in data.items():
        n = len(d['df_1h'])
        idx = int(n * IS_RATIO)
        split_t = d['df_1h'].index[idx]
        is_d[sym] = {
            'df_1h': d['df_1h'].iloc[:idx],
            'df_4h': d['df_4h'][d['df_4h'].index < split_t],
            'df_1d': d['df_1d'][d['df_1d'].index < split_t],
        }
        oos_d[sym] = {
            'df_1h': d['df_1h'].iloc[idx:],
            'df_4h': d['df_4h'][d['df_4h'].index >= split_t],
            'df_1d': d['df_1d'],
        }
    return is_d, oos_d


def print_metrics(label, m):
    a = m.get('active', {})
    s = m.get('shadow', {})
    stopped = ' [STOPPED]' if m.get('fully_stopped') else ''
    print(f"\n  >>> {label}{stopped}")
    print(f"      最終餘額: ${m['final_capital']:,.0f}  最大回撤: {m['max_drawdown_pct']:.2f}%")
    print(f"      Active: {a['total_trades']:>3}筆 勝率{a['win_rate_pct']:5.1f}% PnL{a['total_pnl_usdt']:+8.0f} "
          f"報酬{a['total_return_pct']:+6.2f}% Sharpe{a['sharpe_ratio']:+5.2f} PF={a['profit_factor']}")
    print(f"      Shadow: {s['total_trades']:>3}筆 勝率{s['win_rate_pct']:5.1f}% PnL{s['total_pnl_usdt']:+8.0f}（不影響資金）")


def shadow_for(active):
    return [s for s in TOP20 if s not in active]


def main():
    print("=" * 90)
    print("  TITAN V2 — Aggressive Passive 完整驗證")
    print("=" * 90)
    print(f"  Universe: {[s.split('/')[0] for s in TOP20]}")
    print(f"  Legacy Active List: {[s.split('/')[0] for s in ACTIVE_VALIDATED]}")

    cfg = load_settings()
    ex = Exchange(cfg); ex.connect()
    loader = DataLoader(ex)

    print(f"\n  拉取 {len(TOP20)} 幣資料...")
    all_data = fetch_all(loader)
    print(f"  成功: {len(all_data)} 幣")

    is_data, oos_data = split_oos(all_data)

    strategies_to_test = [
        ('V1-ACTIVE3',        TrendPullback,             ACTIVE_VALIDATED),
        ('V2-SL-075-ACT3',    TrendPullbackV2_SL075,     ACTIVE_VALIDATED),
        ('V2-SL-100-ACT3',    TrendPullbackV2_SL100,     ACTIVE_VALIDATED),
        ('V2-DYN-ACT3',       TrendPullbackV2_DYN,       ACTIVE_VALIDATED),
        ('123-ALL20',         Reversal123,               TOP20),
        ('2B-ALL20',          Fakeout2B,                 TOP20),
        ('V3-REGIME-ALL20',   RegimeMomentumBreakout,    TOP20),
    ]

    results = {}

    for label, data in [('IS', is_data), ('OOS', oos_data)]:
        print(f"\n{'#' * 90}")
        print(f"  {label} 期間")
        print(f"{'#' * 90}")

        results[label] = {}
        for strat_name, strat_cls, active_list in strategies_to_test:
            shadow_list = shadow_for(active_list)
            print(f"\n  跑 {strat_name}...")
            print(f"      Active: {[s.split('/')[0] for s in active_list]}")
            eng = PortfolioEngineV2(
                strategy_factory = lambda c=strat_cls: c(cfg),
                settings = cfg,
                active_list = active_list,
                shadow_list = shadow_list,
            )
            r = eng.run(data)
            results[label][strat_name] = r
            print_metrics(strat_name, r)

    # 終極對照表
    print(f"\n{'=' * 90}")
    print(f"  V2 對照表（OOS）")
    print(f"{'=' * 90}\n")

    print(f"  {'策略':<14} | {'Active 交易':>10} | {'Active 報酬':>11} | {'回撤':>8} | {'Sharpe':>7} | {'餘額':>8}")
    print(f"  {'-'*14} | {'-'*10} | {'-'*11} | {'-'*8} | {'-'*7} | {'-'*8}")
    for strat_name, _, _ in strategies_to_test:
        r = results['OOS'][strat_name]
        a = r['active']
        print(f"  {strat_name:<14} | {a['total_trades']:>10} | "
              f"{a['total_return_pct']:>+10.2f}% | {r['max_drawdown_pct']:>7.2f}% | "
              f"{a['sharpe_ratio']:>+7.2f} | ${r['final_capital']:>7,.0f}")

    # Shadow 統計
    print(f"\n  Shadow List 統計（OOS，未實際開倉，僅參考）：")
    for strat_name, _, _ in strategies_to_test:
        s = results['OOS'][strat_name]['shadow']
        print(f"  {strat_name:<14} | Shadow {s['total_trades']:>3}筆 模擬報酬{s['total_pnl_usdt']:+8.0f}")

    # 上線標準檢查
    print(f"\n{'=' * 90}")
    print(f"  上線標準檢查（OOS）")
    print(f"{'=' * 90}")
    print(f"  - OOS 交易數 ≥ 50")
    print(f"  - 回撤 < 30%")
    print(f"  - 沒爆倉")
    print()
    for strat_name, _, _ in strategies_to_test:
        r = results['OOS'][strat_name]
        a = r['active']
        check_trades = '✓' if a['total_trades'] >= 50 else '✗'
        check_dd = '✓' if r['max_drawdown_pct'] < 30 else '✗'
        check_no_blow = '✓' if not r.get('fully_stopped') else '✗'
        all_pass = check_trades == '✓' and check_dd == '✓' and check_no_blow == '✓'
        verdict = '[達標]' if all_pass else '[未達標]'
        print(f"  {strat_name:<14}: 交易{check_trades} 回撤{check_dd} 未爆倉{check_no_blow} → {verdict}")

    # 儲存
    out = Path('data/v2_validation.json')
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        slim = {}
        for label in results:
            slim[label] = {}
            for sn, r in results[label].items():
                r2 = {k: v for k, v in r.items() if k != 'trades'}
                slim[label][sn] = r2
        json.dump(slim, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[儲存] {out}")


if __name__ == "__main__":
    main()

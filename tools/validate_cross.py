"""
TITAN — V1 vs V1-CROSS-100X 完整對比

兩個版本都跑 20 幣全掃描，但：
  V1：理論上可同時開 20 筆（沒風控）
  V1-CROSS：最多 3 筆 + 完整風控
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
from pathlib import Path

import pandas as pd

from config.settings_loader import load_settings
from core.exchange import Exchange
from strategies.candidates import TrendPullback, TrendPullbackCross100x
from backtest.data_loader import DataLoader
from backtest.engine_portfolio import PortfolioBacktestEngine

TOP20 = [
    'BTC/USDT:USDT', 'ETH/USDT:USDT', 'BNB/USDT:USDT', 'SOL/USDT:USDT',
    'XRP/USDT:USDT', 'ADA/USDT:USDT', 'DOGE/USDT:USDT', 'AVAX/USDT:USDT',
    'TRX/USDT:USDT', 'LINK/USDT:USDT', 'DOT/USDT:USDT', 'LTC/USDT:USDT',
    'BCH/USDT:USDT', 'NEAR/USDT:USDT', 'ATOM/USDT:USDT', 'FIL/USDT:USDT',
    'APT/USDT:USDT',  'ARB/USDT:USDT',  'OP/USDT:USDT',  'SUI/USDT:USDT',
]
DAYS = 365
IS_RATIO = 0.6


def fetch_all(loader):
    """拉所有幣的資料"""
    data = {}
    for sym in TOP20:
        try:
            df_1h = loader.fetch(sym, '1h', days=DAYS)
            df_4h = loader.fetch(sym, '4h', days=DAYS)
            df_1d = loader.fetch(sym, '1d', days=DAYS + 60)
            data[sym] = {'df_1h': df_1h, 'df_4h': df_4h, 'df_1d': df_1d}
        except Exception as e:
            print(f"  跳過 {sym.split('/')[0]}: {e}")
    return data


def split_oos(data):
    """每個幣切 IS / OOS"""
    is_data, oos_data = {}, {}
    for sym, d in data.items():
        n = len(d['df_1h'])
        split_idx = int(n * IS_RATIO)
        split_t = d['df_1h'].index[split_idx]
        is_data[sym] = {
            'df_1h': d['df_1h'].iloc[:split_idx],
            'df_4h': d['df_4h'][d['df_4h'].index < split_t],
            'df_1d': d['df_1d'][d['df_1d'].index < split_t],
        }
        oos_data[sym] = {
            'df_1h': d['df_1h'].iloc[split_idx:],
            'df_4h': d['df_4h'][d['df_4h'].index >= split_t],
            'df_1d': d['df_1d'],  # 完整給箱體初始化
        }
    return is_data, oos_data


def print_metrics(label, m):
    print(f"\n  --- {label} ---")
    print(f"    交易數    : {m['total_trades']}")
    print(f"    勝率      : {m['win_rate_pct']:.1f}%")
    print(f"    最終餘額  : ${m['final_capital']:,.0f}  (起始 5,000)")
    print(f"    報酬率    : {m['total_return_pct']:+.2f}%")
    print(f"    平均贏單  : +${m['avg_win_usdt']:.0f}")
    print(f"    平均輸單  : ${m['avg_loss_usdt']:+.2f}")
    print(f"    最大回撤  : {m['max_drawdown_pct']:.2f}%")
    print(f"    Profit Factor: {m['profit_factor']}")
    print(f"    Sharpe    : {m['sharpe_ratio']:.2f}")


def main():
    print("=" * 84)
    print("  TITAN — V1 vs V1-CROSS-100X (RICK 修正版)  Portfolio 回測")
    print("=" * 84)

    cfg = load_settings()
    ex = Exchange(cfg); ex.connect()
    loader = DataLoader(ex)

    print(f"\n  拉取 {len(TOP20)} 幣資料...")
    all_data = fetch_all(loader)
    print(f"  成功取得 {len(all_data)} 幣")

    is_data, oos_data = split_oos(all_data)

    # 風控設定（RICK 規格）
    risk_cfg_cross = {
        'max_concurrent_positions': 3,
        'max_total_exposure_usdt':  30000,
        'daily_loss_limit_usdt':    -250,
        'consec_loss_threshold':    3,
        'cooldown_minutes':         30,
        'dd_throttle_pct':          10,
        'dd_stop_pct':              20,
    }

    # 寬鬆設定（V1 原版理論：不限同時持倉）
    risk_cfg_v1 = {
        'max_concurrent_positions': 20,
        'max_total_exposure_usdt':  200000,
        'daily_loss_limit_usdt':    -10000,
        'consec_loss_threshold':    100,
        'cooldown_minutes':         0,
        'dd_throttle_pct':          100,
        'dd_stop_pct':              100,
    }

    results = {}

    for label, data in [('IS', is_data), ('OOS', oos_data)]:
        print(f"\n{'#' * 84}")
        print(f"  {label} 期間")
        print(f"{'#' * 84}")

        # V1 (無風控)
        print(f"\n  跑 V1 (無風控)...")
        eng_v1 = PortfolioBacktestEngine(
            strategy_factory = lambda: TrendPullback(cfg),
            settings = cfg,
            risk_cfg = risk_cfg_v1,
        )
        v1_r = eng_v1.run(data)

        # V1-CROSS (RICK 風控)
        print(f"  跑 V1-CROSS-100X (RICK 風控)...")
        eng_cross = PortfolioBacktestEngine(
            strategy_factory = lambda: TrendPullbackCross100x(cfg),
            settings = cfg,
            risk_cfg = risk_cfg_cross,
        )
        cross_r = eng_cross.run(data)

        results[label] = {'v1': v1_r, 'cross': cross_r}

        print_metrics(f"V1 ({label})", v1_r)
        print_metrics(f"V1-CROSS-100X ({label})", cross_r)

    # 終極對比
    print(f"\n{'=' * 84}")
    print(f"  最終對照表")
    print(f"{'=' * 84}\n")

    print(f"  {'指標':<14} | {'V1 IS':>12} | {'V1 OOS':>12} | {'CROSS IS':>12} | {'CROSS OOS':>12}")
    print(f"  {'-'*14} | {'-'*12} | {'-'*12} | {'-'*12} | {'-'*12}")

    for label, key, fmt in [
        ('交易數',     'total_trades',     '{}'),
        ('勝率',       'win_rate_pct',     '{:.1f}%'),
        ('總報酬',     'total_return_pct', '{:+.2f}%'),
        ('最終餘額',   'final_capital',    '${:,.0f}'),
        ('最大回撤',   'max_drawdown_pct', '{:.2f}%'),
        ('Sharpe',     'sharpe_ratio',     '{:.2f}'),
        ('PF',         'profit_factor',    '{}'),
    ]:
        v1_is  = results['IS']['v1'].get(key, 0)
        v1_oos = results['OOS']['v1'].get(key, 0)
        cr_is  = results['IS']['cross'].get(key, 0)
        cr_oos = results['OOS']['cross'].get(key, 0)
        try:
            print(f"  {label:<14} | {fmt.format(v1_is):>12} | {fmt.format(v1_oos):>12} | "
                  f"{fmt.format(cr_is):>12} | {fmt.format(cr_oos):>12}")
        except (ValueError, TypeError):
            print(f"  {label:<14} | {str(v1_is):>12} | {str(v1_oos):>12} | "
                  f"{str(cr_is):>12} | {str(cr_oos):>12}")

    # 儲存
    out = Path('data/cross_validation.json')
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        # 移除 trades list 避免太大
        slim = {}
        for label, sec in results.items():
            slim[label] = {}
            for stratname, r in sec.items():
                r2 = {k: v for k, v in r.items() if k != 'trades'}
                slim[label][stratname] = r2
        json.dump(slim, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[儲存] {out}")


if __name__ == "__main__":
    main()

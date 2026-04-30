"""
TITAN — 三策略 × 市值前 20 大幣種

對每個幣跑：
  1. 朋友箱體（自動偵測，不用手動）
  2. MIA TrendPullback V1
  3. MIA TrendPullback V2

最終對比哪個策略最能「**通用化**」到 20 個幣。
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


TOP20 = [
    'BTC/USDT:USDT', 'ETH/USDT:USDT', 'BNB/USDT:USDT', 'SOL/USDT:USDT',
    'XRP/USDT:USDT', 'ADA/USDT:USDT', 'DOGE/USDT:USDT', 'AVAX/USDT:USDT',
    'TRX/USDT:USDT', 'LINK/USDT:USDT', 'DOT/USDT:USDT', 'LTC/USDT:USDT',
    'BCH/USDT:USDT', 'NEAR/USDT:USDT', 'ATOM/USDT:USDT', 'FIL/USDT:USDT',
    'APT/USDT:USDT',  'ARB/USDT:USDT',  'OP/USDT:USDT',  'SUI/USDT:USDT',
]
DAYS = 365
IS_RATIO = 0.6


def run_one(symbol, cfg, loader):
    try:
        df_1h = loader.fetch(symbol, '1h', days=DAYS)
        df_4h = loader.fetch(symbol, '4h', days=DAYS)
        df_1d = loader.fetch(symbol, '1d', days=DAYS + 30)
    except Exception as e:
        return None

    if len(df_1h) < 200:
        return None

    split_idx = int(len(df_1h) * IS_RATIO)
    split_t = df_1h.index[split_idx]

    is_1h  = df_1h.iloc[:split_idx]
    is_4h  = df_4h[df_4h.index < split_t]
    is_1d  = df_1d[df_1d.index < split_t]

    oos_1h = df_1h.iloc[split_idx:]
    oos_4h = df_4h[df_4h.index >= split_t]
    oos_1d = df_1d  # 完整歷史，給箱體初始化

    out = {}

    # 朋友箱體（自動）
    try:
        eng = V13BacktestEngine(RangeBreakout(cfg), cfg, symbol)
        out['box_oos'] = eng.run(oos_1h, oos_4h, oos_1d, init_box_with_manual=False)
    except Exception:
        out['box_oos'] = None

    # V1
    try:
        out['v1_oos'] = SimpleBacktestEngine(TrendPullback(cfg), cfg, symbol).run(oos_1h, oos_4h, oos_1d)
    except Exception:
        out['v1_oos'] = None

    # V2
    try:
        out['v2_oos'] = SimpleBacktestEngine(TrendPullbackV2(cfg), cfg, symbol).run(oos_1h, oos_4h, oos_1d)
    except Exception:
        out['v2_oos'] = None

    return out


def short_metric(r):
    if r is None:
        return {'ret': 0, 'trades': 0, 'sharpe': 0, 'dd': 0}
    return {
        'ret':    float(r.get('total_return_pct', 0)),
        'trades': int(r.get('total_trades', 0)),
        'sharpe': float(r.get('sharpe_ratio', 0)),
        'dd':     float(r.get('max_drawdown_pct', 0)),
    }


def main():
    print("=" * 90)
    print("  三策略 x 前 20 大幣種 OOS 對比")
    print("=" * 90)

    cfg = load_settings()
    ex = Exchange(cfg); ex.connect()
    loader = DataLoader(ex)

    rows = []
    for i, symbol in enumerate(TOP20, 1):
        coin = symbol.split('/')[0]
        print(f"  [{i:2d}/{len(TOP20)}] {coin:<6} ...", end=' ', flush=True)
        result = run_one(symbol, cfg, loader)
        if result is None:
            print("SKIP")
            continue

        box = short_metric(result['box_oos'])
        v1  = short_metric(result['v1_oos'])
        v2  = short_metric(result['v2_oos'])

        rows.append({
            'coin': coin,
            'box': box, 'v1': v1, 'v2': v2,
        })
        print(f"box:{box['ret']:+5.1f}% v1:{v1['ret']:+5.1f}% v2:{v2['ret']:+5.1f}%")

    # 表格輸出
    print()
    print("=" * 90)
    print("  逐幣 OOS 對比（報酬 / 交易數 / Sharpe / 回撤）")
    print("=" * 90)
    print()
    print(f"  {'幣':<6} | {'朋友箱體':<25} | {'回調 V1':<25} | {'回調 V2':<25}")
    print(f"  {'-'*6} | {'-'*25} | {'-'*25} | {'-'*25}")
    for r in rows:
        cells = []
        for k in ['box', 'v1', 'v2']:
            d = r[k]
            cells.append(f"{d['ret']:+6.2f}% / {d['trades']:>3}t / Sh{d['sharpe']:+5.2f}")
        print(f"  {r['coin']:<6} | {cells[0]:<25} | {cells[1]:<25} | {cells[2]:<25}")

    # 整體統計
    print()
    print("=" * 90)
    print("  整體統計")
    print("=" * 90)

    n = len(rows)
    for k, label in [('box', '朋友箱體'), ('v1', '回調 V1'), ('v2', '回調 V2')]:
        rets = [r[k]['ret'] for r in rows]
        wins = sum(1 for x in rets if x > 0)
        total = sum(rets)
        avg = total / n
        trades = sum(r[k]['trades'] for r in rows)
        avg_sharpe = sum(r[k]['sharpe'] for r in rows) / n
        max_dd = max(r[k]['dd'] for r in rows)
        print(f"\n  【{label}】")
        print(f"    OOS 賺錢       : {wins}/{n} ({wins/n*100:.0f}%)")
        print(f"    OOS 平均報酬   : {avg:+.2f}%")
        print(f"    OOS 報酬加總   : {total:+.2f}%")
        print(f"    OOS 平均 Sharpe: {avg_sharpe:+.2f}")
        print(f"    OOS 總交易數   : {trades}")
        print(f"    OOS 最大回撤   : {max_dd:.1f}%")

    # 換算
    print()
    print("=" * 90)
    print("  20 幣等權重組合（每幣 250U）每月預期報酬")
    print("=" * 90)
    for k, label in [('box', '朋友箱體'), ('v1', '回調 V1'), ('v2', '回調 V2')]:
        avg = sum(r[k]['ret'] for r in rows) / n
        per_month = avg / 5  # OOS 約 5 個月
        per_day_usdt = (avg / 100) * (5000 / n) * n / (5 * 30)
        print(f"  {label:<10}: 平均{avg:+6.2f}% (5月) = {per_month:+5.2f}%/月 = ~${per_day_usdt:.2f}/日")

    out = Path('data/top20_strategies.json')
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(rows, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[儲存] {out}")


if __name__ == "__main__":
    main()

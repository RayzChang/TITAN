"""
TITAN — 朋友箱體策略 × 市值前 20 大幣種掃描

對每個幣自動偵測箱體（無 manual_boxes），跑 IS + OOS 對比。
最終排序：哪些幣這策略最賺、哪些最不適合。
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
from pathlib import Path

import pandas as pd

from config.settings_loader import load_settings
from core.exchange import Exchange
from strategies.range_breakout import RangeBreakout
from backtest.data_loader import DataLoader
from backtest.engine_v13 import V13BacktestEngine


# 市值前 20 大（排除穩定幣，按 CoinMarketCap 大略順序）
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
    """跑一個幣的 IS + OOS。失敗回傳 None"""
    try:
        df_1h = loader.fetch(symbol, '1h', days=DAYS)
        df_4h = loader.fetch(symbol, '4h', days=DAYS)
        df_1d = loader.fetch(symbol, '1d', days=DAYS + 30)
    except Exception as e:
        return {'error': f'data fetch failed: {e}'}

    if len(df_1h) < 200:
        return {'error': f'insufficient data ({len(df_1h)} bars)'}

    split_idx = int(len(df_1h) * IS_RATIO)
    split_t = df_1h.index[split_idx]

    # IS：時間範圍內的子集
    is_1h = df_1h.iloc[:split_idx]
    is_4h = df_4h[df_4h.index < split_t]
    is_1d = df_1d[df_1d.index < split_t]

    # OOS：1H/4H 切後段，但 1D 保留全部歷史（給箱體初始化用）
    oos_1h = df_1h.iloc[split_idx:]
    oos_4h = df_4h[df_4h.index >= split_t]
    oos_1d = df_1d  # 完整歷史 → 箱體建得起來

    try:
        eng_is = V13BacktestEngine(RangeBreakout(cfg), cfg, symbol)
        is_r = eng_is.run(is_1h, is_4h, is_1d, init_box_with_manual=False)
        eng_oos = V13BacktestEngine(RangeBreakout(cfg), cfg, symbol)
        oos_r = eng_oos.run(oos_1h, oos_4h, oos_1d, init_box_with_manual=False)
    except Exception as e:
        return {'error': f'backtest failed: {e}'}

    return {'is': is_r, 'oos': oos_r}


def main():
    print("=" * 80)
    print("  朋友箱體策略 × 市值前 20 大幣種")
    print("=" * 80)

    cfg = load_settings()
    ex = Exchange(cfg); ex.connect()
    loader = DataLoader(ex)

    rows = []
    for i, symbol in enumerate(TOP20, 1):
        coin = symbol.split('/')[0]
        print(f"  [{i:2d}/{len(TOP20)}] {coin:<6} ...", end=' ', flush=True)
        result = run_one(symbol, cfg, loader)

        if 'error' in result:
            print(f"SKIP ({result['error']})")
            continue

        is_r = result['is']
        oos_r = result['oos']
        rows.append({
            'coin': coin,
            'is_trades':  is_r.get('total_trades', 0),
            'is_ret':     is_r.get('total_return_pct', 0),
            'is_sharpe':  is_r.get('sharpe_ratio', 0),
            'is_dd':      is_r.get('max_drawdown_pct', 0),
            'oos_trades': oos_r.get('total_trades', 0),
            'oos_ret':    oos_r.get('total_return_pct', 0),
            'oos_sharpe': oos_r.get('sharpe_ratio', 0),
            'oos_dd':     oos_r.get('max_drawdown_pct', 0),
            'oos_wr':     oos_r.get('win_rate_pct', 0),
        })
        print(f"IS:{is_r.get('total_return_pct', 0):+6.2f}% / OOS:{oos_r.get('total_return_pct', 0):+6.2f}%")

    if not rows:
        print("\n沒有任何幣有結果，可能是資料拉不到")
        return

    # 排序：OOS 報酬高 → 低
    rows.sort(key=lambda r: r['oos_ret'], reverse=True)

    print()
    print("=" * 80)
    print("  最終排名（按 OOS 報酬排序）")
    print("=" * 80)
    print()
    print(f"  {'幣':<6} | {'IS交易':>6} {'IS報酬':>8} {'IS夏普':>7} {'IS回撤':>7} | "
          f"{'OOS交易':>7} {'OOS報酬':>9} {'OOS夏普':>8} {'OOS回撤':>8} {'OOS勝率':>8}")
    print(f"  {'-'*6} | {'-'*6} {'-'*8} {'-'*7} {'-'*7} | "
          f"{'-'*7} {'-'*9} {'-'*8} {'-'*8} {'-'*8}")
    for r in rows:
        print(
            f"  {r['coin']:<6} | "
            f"{r['is_trades']:>6} {r['is_ret']:>+7.2f}% {r['is_sharpe']:>+7.2f} {r['is_dd']:>6.1f}% | "
            f"{r['oos_trades']:>7} {r['oos_ret']:>+8.2f}% {r['oos_sharpe']:>+8.2f} "
            f"{r['oos_dd']:>7.1f}% {r['oos_wr']:>7.1f}%"
        )

    # 統計
    print()
    print("=" * 80)
    print("  整體統計")
    print("=" * 80)

    profitable_oos = [r for r in rows if r['oos_ret'] > 0]
    losing_oos     = [r for r in rows if r['oos_ret'] <= 0]
    total_oos_ret  = sum(r['oos_ret'] for r in rows)
    avg_oos_ret    = total_oos_ret / len(rows) if rows else 0
    total_oos_trades = sum(r['oos_trades'] for r in rows)

    print(f"  測試幣數    : {len(rows)} 個")
    print(f"  OOS 賺錢   : {len(profitable_oos)} 個 ({len(profitable_oos)/len(rows)*100:.0f}%)")
    print(f"  OOS 虧錢   : {len(losing_oos)} 個")
    print(f"  OOS 平均報酬: {avg_oos_ret:+.2f}%")
    print(f"  OOS 總交易數: {total_oos_trades} 筆")
    print(f"  OOS 平均交易: {total_oos_trades / len(rows):.1f} 筆/幣")

    # 假設等權重組合
    print()
    print(f"  >> 若 20 幣等權重組合（每幣 250U/5000U）:")
    print(f"    OOS 加權平均報酬 = {avg_oos_ret:+.2f}%")
    print(f"    換算每月 = {avg_oos_ret/5:+.2f}% (5 個月期間)")

    # 儲存
    out = Path('data/top20_results.json')
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(rows, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[儲存] {out}")


if __name__ == "__main__":
    main()

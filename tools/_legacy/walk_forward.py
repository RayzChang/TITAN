"""
TITAN — V1 Walk-Forward 驗證

把 365 天切成 5 段（每段約 73 天），跑滑動驗證：
  測試段 1: Day 0~73   (純 OOS)
  測試段 2: Day 73~146 (純 OOS)
  測試段 3: Day 146~219(純 OOS)
  測試段 4: Day 219~292(純 OOS)
  測試段 5: Day 292~365(純 OOS)

每段都跑一次 V1，看是否每段都正報酬、Sharpe 穩定。
若 5 段中有 4 段以上正報酬 → 真穩定
若波動大、有負報酬 → 不穩定
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
from pathlib import Path

import pandas as pd

from config.settings_loader import load_settings
from core.exchange import Exchange
from strategies.candidates import TrendPullback
from backtest.data_loader import DataLoader
from backtest.engine_simple import SimpleBacktestEngine

SYMBOLS = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'DOGE/USDT:USDT', 'ATOM/USDT:USDT']
TOTAL_DAYS = 365
N_FOLDS = 5


def run_one(symbol, cfg, loader):
    print(f"\n{'─' * 70}")
    print(f"  {symbol}")
    print(f"{'─' * 70}")

    df_1h = loader.fetch(symbol, '1h', days=TOTAL_DAYS)
    df_4h = loader.fetch(symbol, '4h', days=TOTAL_DAYS)

    n = len(df_1h)
    fold_size = n // N_FOLDS

    folds = []
    for k in range(N_FOLDS):
        start = k * fold_size
        end = (k + 1) * fold_size if k < N_FOLDS - 1 else n

        seg_1h = df_1h.iloc[start:end]
        if len(seg_1h) < 100:
            continue
        seg_start_t = seg_1h.index[0]
        seg_end_t   = seg_1h.index[-1]
        seg_4h = df_4h[(df_4h.index >= seg_start_t) & (df_4h.index <= seg_end_t)]

        try:
            eng = SimpleBacktestEngine(TrendPullback(cfg), cfg, symbol)
            r = eng.run(seg_1h, seg_4h, None)
            ret = r['total_return_pct']
            sharpe = r['sharpe_ratio']
            trades = r['total_trades']
            wr = r['win_rate_pct']
            dd = r['max_drawdown_pct']
            folds.append({
                'fold': k + 1,
                'start': str(seg_start_t),
                'end':   str(seg_end_t),
                'trades': trades,
                'win_rate_pct': wr,
                'return_pct':   ret,
                'sharpe':       sharpe,
                'max_dd_pct':   dd,
            })
            print(f"  段 {k+1} | {seg_start_t.date()} ~ {seg_end_t.date()} | "
                  f"交易{trades:3d} 勝率{wr:5.1f}% 報酬{ret:+7.2f}% Sharpe{sharpe:+5.2f} DD{dd:5.1f}%")
        except Exception as e:
            print(f"  段 {k+1}: 失敗 - {e}")

    if not folds:
        return None

    # 統計
    rets    = [f['return_pct']  for f in folds]
    sharpes = [f['sharpe']      for f in folds]
    positive = sum(1 for r in rets if r > 0)

    print(f"\n  {'-'*40}")
    print(f"  正報酬段: {positive}/{len(folds)}")
    print(f"  平均報酬: {sum(rets)/len(rets):+.2f}%")
    print(f"  最低段:   {min(rets):+.2f}%")
    print(f"  最高段:   {max(rets):+.2f}%")
    print(f"  Sharpe 範圍: {min(sharpes):+.2f} ~ {max(sharpes):+.2f}")

    # 結論
    if positive >= 4:
        verdict = "[穩定] 5 段中 4+ 段賺錢，策略可靠"
    elif positive >= 3:
        verdict = "[尚可] 5 段中 3 段賺錢，邊緣穩定"
    elif positive >= 2:
        verdict = "[不穩] 5 段中只 2 段賺錢，依賴特定市場"
    else:
        verdict = "[失敗] 5 段中 < 2 段賺錢，策略可能無 edge"

    print(f"  >>> {verdict}")

    return {
        'symbol': symbol,
        'folds': folds,
        'positive_folds': positive,
        'avg_return': sum(rets)/len(rets),
        'verdict': verdict,
    }


def main():
    print("=" * 72)
    print("  TITAN — V1 Walk-Forward 驗證")
    print("=" * 72)
    print(f"  切分: {TOTAL_DAYS} 天分 {N_FOLDS} 段，每段獨立跑")

    cfg = load_settings()
    ex = Exchange(cfg); ex.connect()
    loader = DataLoader(ex)

    all_results = []
    for symbol in SYMBOLS:
        r = run_one(symbol, cfg, loader)
        if r:
            all_results.append(r)

    # 總表
    print()
    print("=" * 72)
    print("  Walk-Forward 總結")
    print("=" * 72)
    print()
    print(f"  {'幣':<6} | {'段1':>8} {'段2':>8} {'段3':>8} {'段4':>8} {'段5':>8} | {'正報酬':>6} | {'結論':<35}")
    print(f"  {'-'*6} | {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*8} | {'-'*6} | {'-'*35}")
    for r in all_results:
        coin = r['symbol'].split('/')[0]
        rets_s = []
        for fold in r['folds']:
            rets_s.append(f"{fold['return_pct']:+7.2f}%")
        # 補不足 5 段
        while len(rets_s) < 5:
            rets_s.append(f"{'N/A':>8}")
        print(
            f"  {coin:<6} | {rets_s[0]:>8} {rets_s[1]:>8} {rets_s[2]:>8} "
            f"{rets_s[3]:>8} {rets_s[4]:>8} | {r['positive_folds']:>3}/{len(r['folds'])}"
            f"  | {r['verdict']:<35}"
        )

    out = Path('data/walkforward_results.json')
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[儲存] {out}")


if __name__ == "__main__":
    main()

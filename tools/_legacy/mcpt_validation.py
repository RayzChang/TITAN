"""
TITAN — MCPT (Monte Carlo Permutation Test) 驗證 V1 策略

方法（Random Entry MCPT）：
  1. 在 OOS 期間跑真實 V1 → 記錄 N 筆交易與總 PnL
  2. 模擬 1000 次「隨機策略」：
     - 在 OOS 期間隨機選 N 個進場時機（時間點隨機）
     - 進場方向隨機（多/空各半）
     - 用同樣的 SL/TP 規則計算結果
  3. 比較：V1 真實 PnL 排在 1000 次隨機結果的哪個分位
     - p < 0.05 (top 5%)  → 顯著有 edge ✓
     - p > 0.05            → 是運氣，不算有 edge ✗
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import random
import math
from pathlib import Path

import numpy as np
import pandas as pd

from config.settings_loader import load_settings
from core.exchange import Exchange
from strategies.candidates import TrendPullback
from backtest.data_loader import DataLoader
from backtest.engine_simple import SimpleBacktestEngine

SYMBOLS = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'DOGE/USDT:USDT', 'ATOM/USDT:USDT']
DAYS = 365
IS_RATIO = 0.6
MCPT_RUNS = 1000   # 模擬次數
TAKER_FEE = 0.0005


def get_real_v1_results(symbol, df_1h, df_4h, cfg):
    """跑真實 V1，回傳交易列表 + 總 PnL"""
    eng = SimpleBacktestEngine(TrendPullback(cfg), cfg, symbol)
    result = eng.run(df_1h, df_4h, None)
    return result['trades'], result['total_return_pct'], result['sharpe_ratio']


def simulate_random_strategy(df_1h, n_trades, position_usdt, leverage):
    """
    模擬一次「隨機進場」策略：
      - 在 1H K 線範圍內隨機選 n_trades 個進場時間
      - 方向隨機（50/50）
      - 用「進場後 5 根 K 線」當 SL/TP 觸發窗口
        SL 距離 = 0.01（1%），TP 距離 = 0.02（2%）模仿 V1 平均 R-multiple
    """
    if len(df_1h) < 50 or n_trades == 0:
        return 0.0

    # 隨機選 n_trades 個進場時間（避開最後 10 根，留出 SL/TP 觸發空間）
    indices = random.sample(range(50, len(df_1h) - 10), min(n_trades, len(df_1h) - 60))
    total_pnl = 0.0

    for i in indices:
        entry = float(df_1h.iloc[i]['open'])
        side = random.choice(['LONG', 'SHORT'])

        sl_pct = 0.01  # V1 平均 SL 距離約 1%
        tp_pct = 0.02

        if side == 'LONG':
            sl_price = entry * (1 - sl_pct)
            tp_price = entry * (1 + tp_pct)
        else:
            sl_price = entry * (1 + sl_pct)
            tp_price = entry * (1 - tp_pct)

        # 看接下來 10 根 K 線哪個先觸發
        exit_price = entry  # 預設打平
        for j in range(i + 1, min(i + 11, len(df_1h))):
            bar = df_1h.iloc[j]
            if side == 'LONG':
                if bar['low'] <= sl_price:
                    exit_price = sl_price; break
                if bar['high'] >= tp_price:
                    exit_price = tp_price; break
            else:
                if bar['high'] >= sl_price:
                    exit_price = sl_price; break
                if bar['low'] <= tp_price:
                    exit_price = tp_price; break
        else:
            exit_price = float(df_1h.iloc[min(i + 10, len(df_1h) - 1)]['close'])

        if side == 'LONG':
            raw = (exit_price - entry) / entry
        else:
            raw = (entry - exit_price) / entry
        net = raw * leverage - TAKER_FEE * 2
        total_pnl += position_usdt * net

    return total_pnl


def mcpt_one_symbol(symbol, cfg, loader):
    print(f"\n{'─' * 70}")
    print(f"  {symbol}")
    print(f"{'─' * 70}")

    df_1h = loader.fetch(symbol, '1h', days=DAYS)
    df_4h = loader.fetch(symbol, '4h', days=DAYS)

    split_idx = int(len(df_1h) * IS_RATIO)
    split_t = df_1h.index[split_idx]
    oos_1h = df_1h.iloc[split_idx:]
    oos_4h = df_4h[df_4h.index >= split_t]

    # 真實 V1
    print(f"  [1/2] 跑真實 V1 OOS...")
    real_trades, real_ret_pct, real_sharpe = get_real_v1_results(
        symbol, oos_1h, oos_4h, cfg
    )
    n_trades = len(real_trades)
    real_pnl = sum(t['pnl_usdt'] for t in real_trades)
    print(f"    實際交易數: {n_trades}")
    print(f"    實際總 PnL: {real_pnl:+.2f} USDT")
    print(f"    實際報酬率: {real_ret_pct:+.2f}%")

    if n_trades == 0:
        print(f"    [SKIP] 無交易，無法做 MCPT")
        return None

    # MCPT 模擬
    position_usdt = float(cfg.get('capital', {}).get('position_fixed_usdt', 100))
    leverage      = int(cfg.get('risk', {}).get('leverage', 100))

    print(f"  [2/2] 跑 {MCPT_RUNS} 次隨機策略模擬...")
    random.seed(42)  # 重現性
    random_pnls = []
    for run in range(MCPT_RUNS):
        if run % 100 == 0:
            print(f"    進度 {run}/{MCPT_RUNS}...", end='\r')
        rand_pnl = simulate_random_strategy(oos_1h, n_trades, position_usdt, leverage)
        random_pnls.append(rand_pnl)

    random_pnls = np.array(random_pnls)
    n_better = int((random_pnls >= real_pnl).sum())
    p_value = n_better / MCPT_RUNS

    print(f"    隨機策略 PnL 分布:")
    print(f"      平均  = {random_pnls.mean():+.2f}")
    print(f"      中位數 = {np.median(random_pnls):+.2f}")
    print(f"      最大  = {random_pnls.max():+.2f}")
    print(f"      最小  = {random_pnls.min():+.2f}")

    print(f"\n  >>> V1 PnL ({real_pnl:+.2f}) 贏過 {(1-p_value)*100:.1f}% 隨機策略")
    print(f"  >>> p-value = {p_value:.4f}")

    if p_value < 0.05:
        verdict = "[通過] V1 有顯著 edge (p < 0.05)"
    elif p_value < 0.20:
        verdict = "[邊緣] V1 略勝隨機，但未達顯著水準 (0.05 <= p < 0.20)"
    else:
        verdict = "[不通過] V1 與隨機差不多，可能是運氣 (p >= 0.20)"
    print(f"  >>> 結論: {verdict}")

    return {
        'symbol': symbol,
        'n_trades': n_trades,
        'real_pnl': real_pnl,
        'real_ret_pct': real_ret_pct,
        'real_sharpe': real_sharpe,
        'random_mean': float(random_pnls.mean()),
        'random_median': float(np.median(random_pnls)),
        'random_max': float(random_pnls.max()),
        'random_min': float(random_pnls.min()),
        'p_value': p_value,
        'percentile': float((1 - p_value) * 100),
        'verdict': verdict,
    }


def main():
    print("=" * 72)
    print("  TITAN — V1 (TrendPullback) MCPT 驗證")
    print("=" * 72)
    print(f"  方法: Random Entry MCPT")
    print(f"  模擬次數: {MCPT_RUNS}")
    print(f"  幣種: {len(SYMBOLS)} 個")

    cfg = load_settings()
    ex = Exchange(cfg); ex.connect()
    loader = DataLoader(ex)

    results = []
    for symbol in SYMBOLS:
        r = mcpt_one_symbol(symbol, cfg, loader)
        if r:
            results.append(r)

    # 總結
    print()
    print("=" * 72)
    print("  MCPT 總結")
    print("=" * 72)
    print()
    print(f"  {'幣':<6} | {'V1 PnL':>10} | {'隨機平均':>10} | {'p-value':>8} | {'結論':<30}")
    print(f"  {'-'*6} | {'-'*10} | {'-'*10} | {'-'*8} | {'-'*30}")
    for r in results:
        coin = r['symbol'].split('/')[0]
        print(
            f"  {coin:<6} | {r['real_pnl']:>+9.2f} | {r['random_mean']:>+9.2f} | "
            f"{r['p_value']:>8.4f} | {r['verdict']:<30}"
        )

    passed = sum(1 for r in results if r['p_value'] < 0.05)
    edge   = sum(1 for r in results if r['p_value'] < 0.20)
    print()
    print(f"  顯著通過 (p < 0.05): {passed}/{len(results)}")
    print(f"  邊緣 (p < 0.20)    : {edge}/{len(results)}")

    out = Path('data/mcpt_results.json')
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[儲存] {out}")


if __name__ == "__main__":
    main()

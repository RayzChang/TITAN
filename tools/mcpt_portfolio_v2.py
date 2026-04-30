"""
Portfolio-level MCPT for TITAN V2 research.

This keeps the same PortfolioEngineV2 cost model and portfolio gates, then
compares each real alpha against random entry/side strategies with a calibrated
signal frequency. It is a benchmark, not a proof of profitability.
"""

import json
import os
import random
import sys
from pathlib import Path

import numpy as np

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
DAYS = 365
IS_RATIO = 0.6
MCPT_RUNS = int(os.environ.get('TITAN_MCPT_RUNS', '300'))


class RandomPortfolioStrategy:
    def __init__(self, settings, seed: int, p_signal: float,
                 sl_pct: float = 0.0075, tp_r: float = 1.5):
        self.seed = seed
        self.p_signal = p_signal
        self.sl_pct = sl_pct
        self.tp_r = tp_r
        self._rngs = {}
        self._last_sl_distance = sl_pct

    def _rng(self, symbol: str):
        if symbol not in self._rngs:
            stable = sum((i + 1) * ord(ch) for i, ch in enumerate(symbol))
            self._rngs[symbol] = random.Random(self.seed + stable)
        return self._rngs[symbol]

    def update_data(self, df_4h, df_1d=None):
        pass

    def update_market_context(self, context: dict, symbol: str = ''):
        pass

    def calculate_signal_with_score(self, df, symbol: str = ''):
        if df is None or len(df) < 65:
            return 'HOLD', 0.0, 0.0
        rng = self._rng(symbol)
        if rng.random() > self.p_signal:
            return 'HOLD', 0.0, 0.0
        sig = 'LONG' if rng.random() < 0.5 else 'SHORT'
        self._last_sl_distance = self.sl_pct
        return sig, rng.random(), self.sl_pct

    def calculate_signals(self, df, symbol: str = ''):
        sig, _, _ = self.calculate_signal_with_score(df, symbol)
        return sig

    def get_stop_loss(self, entry_price: float, signal: str, symbol: str = '') -> float:
        d = self._last_sl_distance
        return round(entry_price * (1 - d), 8) if signal == 'LONG' else round(entry_price * (1 + d), 8)

    def get_take_profit(self, entry_price: float, signal: str, symbol: str = '') -> float:
        d = self._last_sl_distance * self.tp_r
        return round(entry_price * (1 + d), 8) if signal == 'LONG' else round(entry_price * (1 - d), 8)


def fetch_all(loader):
    data = {}
    for sym in TOP20:
        try:
            data[sym] = {
                'df_1h': loader.fetch(sym, '1h', days=DAYS),
                'df_4h': loader.fetch(sym, '4h', days=DAYS),
                'df_1d': loader.fetch(sym, '1d', days=DAYS + 60),
            }
        except Exception as exc:
            print(f"[WARN] skip {sym}: {exc}")
    return data


def split_oos(data):
    out = {}
    for sym, d in data.items():
        n = len(d['df_1h'])
        idx = int(n * IS_RATIO)
        split_t = d['df_1h'].index[idx]
        out[sym] = {
            'df_1h': d['df_1h'].iloc[idx:],
            # Include all higher timeframe history; the engine only exposes
            # rows older than each simulated timestamp to the strategy.
            'df_4h': d['df_4h'][d['df_4h'].index < d['df_1h'].index[-1]],
            'df_1d': d['df_1d'],
        }
        _ = split_t
    return out


def shadow_for(active):
    return [s for s in TOP20 if s not in active]


def run_engine(strategy_cls, cfg, data, active):
    eng = PortfolioEngineV2(
        strategy_factory=lambda c=strategy_cls: c(cfg),
        settings=cfg,
        active_list=active,
        shadow_list=shadow_for(active),
    )
    return eng.run(data)


def run_random(cfg, data, active, seed, p_signal):
    eng = PortfolioEngineV2(
        strategy_factory=lambda s=seed, p=p_signal: RandomPortfolioStrategy(cfg, s, p),
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

    all_data = fetch_all(loader)
    oos_data = split_oos(all_data)
    if not oos_data:
        raise RuntimeError('no OOS data loaded')

    strategies = [
        ('V1-ACTIVE3', TrendPullback, ACTIVE_VALIDATED),
        ('V2-SL-075-ACT3', TrendPullbackV2_SL075, ACTIVE_VALIDATED),
        ('V2-SL-100-ACT3', TrendPullbackV2_SL100, ACTIVE_VALIDATED),
        ('123-ALL20', Reversal123, TOP20),
        ('2B-ALL20', Fakeout2B, TOP20),
        ('V3-REGIME-ALL20', RegimeMomentumBreakout, TOP20),
    ]

    ref = next(iter(oos_data.values()))['df_1h']
    bars = max(len(ref), 1)
    results = []

    for name, cls, active in strategies:
        print(f"\n=== {name} ===")
        real = run_engine(cls, cfg, oos_data, active)
        real_pnl = real['active']['total_pnl_usdt']
        real_trades = real['active']['total_trades']
        print(f"real pnl={real_pnl:+.2f} trades={real_trades}")
        if real_trades <= 0:
            continue

        p_signal = min(0.05, max(0.0002, real_trades / max(bars * len(active), 1) * 2.0))
        random_pnls = []
        for i in range(MCPT_RUNS):
            r = run_random(cfg, oos_data, active, seed=10_000 + i, p_signal=p_signal)
            random_pnls.append(r['active']['total_pnl_usdt'])
            if (i + 1) % 50 == 0:
                print(f"  random {i + 1}/{MCPT_RUNS}", end='\r')

        arr = np.array(random_pnls)
        p_value = float((arr >= real_pnl).sum() / len(arr))
        row = {
            'strategy': name,
            'active_count': len(active),
            'real_pnl': real_pnl,
            'real_trades': real_trades,
            'p_signal': p_signal,
            'random_mean': float(arr.mean()),
            'random_median': float(np.median(arr)),
            'random_max': float(arr.max()),
            'random_min': float(arr.min()),
            'p_value': p_value,
        }
        results.append(row)
        print(f"\np={p_value:.4f} random_mean={row['random_mean']:+.2f}")

    out = Path('data/mcpt_portfolio_v2.json')
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(f"\n[written] {out}")


if __name__ == '__main__':
    main()

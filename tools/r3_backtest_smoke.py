"""R3 Sprint 6 backtest smoke runner.

Runs a short real-data simulation to verify that the backtest engine, report
writers, and cost simulators work end to end. This is not an L0-L6 research
check and does not judge strategy quality.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.r3.backtest_engine import BacktestEngine
from strategies.r3.config_loader import R3Config
from strategies.r3.data_loader import R3DataLoader, _default_cache_dir
from strategies.r3.exchange import R3ExchangeData


SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
TIMEFRAMES = ["5m", "1h", "4h"]
UTC = timezone.utc


def main() -> int:
    print("=" * 80)
    print("  R3 Sprint 6 - Backtest Smoke")
    print("=" * 80)

    cfg = R3Config.load()
    lookback_days = int(cfg.backtest.reports.smoke_lookback_days)
    end = datetime.now(UTC)
    start = end - timedelta(days=lookback_days)
    stamp = end.strftime("%Y%m%d_%H%M%S")
    output_dir = Path(str(cfg.backtest.reports.output_root)) / stamp

    print(f"  Range       : {start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M} UTC")
    print(f"  Symbols     : {', '.join(SYMBOLS)}")
    print(f"  Output path : {output_dir}")

    cache_dir = _default_cache_dir()
    loader = R3DataLoader(cfg, cache_dir=cache_dir)
    exchange = R3ExchangeData(cfg, cache_dir=cache_dir)

    data_by_symbol = {}
    funding_by_symbol = {}
    premium_by_symbol = {}
    for symbol in SYMBOLS:
        data_by_symbol[symbol] = {
            timeframe: loader.load_ohlcv(symbol, timeframe, start=start, end=end)
            for timeframe in TIMEFRAMES
        }
        funding_by_symbol[symbol] = exchange.fetch_funding_history(symbol, start=start, end=end)
        premium_by_symbol[symbol] = exchange.fetch_premium_index_klines(
            symbol,
            "1h",
            start=start,
            end=end,
        )

    result = BacktestEngine(cfg).run(
        data_by_symbol=data_by_symbol,
        funding_by_symbol=funding_by_symbol,
        premium_by_symbol=premium_by_symbol,
        output_dir=output_dir,
    )

    required = [
        "trade_log",
        "daily_pnl",
        "equity_curve",
        "drawdown_curve",
        "metrics",
        "report",
    ]
    missing = [name for name in required if name not in result.report_paths or not result.report_paths[name].exists()]
    if missing:
        print(f"  [FAIL] Missing report files: {missing}")
        return 1

    print("\n" + "-" * 80)
    print("  Summary")
    print("-" * 80)
    print(f"  final_equity       : {result.metrics['final_equity']:.2f}")
    print(f"  total_return_pct   : {result.metrics['total_return_pct']:.2f}")
    print(f"  max_drawdown_pct   : {result.metrics['max_drawdown_pct']:.2f}")
    print(f"  total_trades       : {result.metrics['total_trades']}")
    print(f"  report_files       : {len(result.report_paths)}")
    print("  status             : PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

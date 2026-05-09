"""R3 Sprint 7 validation pipeline runner.

This command runs validation reports only. It does not start dry-run, live
trading, or any exchange order path.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.r3.config_loader import R3Config
from strategies.r3.data_loader import R3DataLoader, _default_cache_dir
from strategies.r3.exchange import R3ExchangeData
from strategies.r3.validation.common import (
    CONCLUSION_APPROVED,
    CONCLUSION_SMOKE,
    VALIDATION_TARGETS,
)
from strategies.r3.validation.validator import R3Validator


SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
TIMEFRAMES = ["5m", "1h", "4h"]
UTC = timezone.utc


def main() -> int:
    args = _parse_args()
    cfg = R3Config.load()
    start, end = _resolve_range(args, cfg)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    output_dir = Path(args.output_dir) / stamp

    print("=" * 80)
    print("  R3 Sprint 7 - L0-L6 Validation")
    print("=" * 80)
    print(f"  Mode        : {args.mode}")
    print(f"  Target      : {args.target}")
    print(f"  Smoke only  : {args.max_runtime_smoke}")
    print(f"  Range       : {start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M} UTC")
    print(f"  Symbols     : {', '.join(args.symbols)}")
    print(f"  Simulations : {args.simulations}")
    print(f"  Output path : {output_dir}")

    data_by_symbol, funding_by_symbol, premium_by_symbol = _load_data(cfg, args.symbols, start, end)
    result = R3Validator(cfg).run(
        mode=args.mode,
        target=args.target,
        symbols=args.symbols,
        initial_capital=args.initial_capital,
        output_dir=output_dir,
        simulations=args.simulations,
        seed=args.seed,
        max_runtime_smoke=args.max_runtime_smoke,
        start=start,
        end=end,
        data_by_symbol=data_by_symbol,
        funding_by_symbol=funding_by_symbol,
        premium_by_symbol=premium_by_symbol,
    )

    print("\n" + "-" * 80)
    print("  Summary")
    print("-" * 80)
    print(f"  conclusion   : {result.conclusion}")
    print(f"  targets      : {', '.join(result.targets)}")
    print(f"  report_files : {len(result.artifacts)}")
    for key, path in result.artifacts.items():
        print(f"  {key:<18}: {path}")

    if args.max_runtime_smoke:
        if result.conclusion != CONCLUSION_SMOKE:
            print("  status       : FAIL")
            return 1
        print("  status       : PASS")
        return 0
    if result.conclusion == CONCLUSION_APPROVED:
        print("  status       : PASS")
        return 0
    print("  status       : REJECTED_OR_INSUFFICIENT_DATA")
    return 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run R3 L0-L6 validation reports.")
    parser.add_argument("--mode", choices=["diagnostic", "gated"], default="diagnostic")
    parser.add_argument("--target", choices=[*VALIDATION_TARGETS, "all"], default="full_r3_portfolio")
    parser.add_argument("--symbols", nargs="+", default=SYMBOLS)
    parser.add_argument("--initial-capital", type=float, default=5000.0)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--output-dir", default="reports/validation/R3")
    parser.add_argument("--max-runtime-smoke", action="store_true")
    parser.add_argument("--simulations", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=7)
    return parser.parse_args()


def _resolve_range(args: argparse.Namespace, cfg: R3Config) -> tuple[datetime, datetime]:
    end = _parse_utc_datetime(args.end) if args.end else datetime.now(UTC)
    if args.start:
        start = _parse_utc_datetime(args.start)
    elif args.max_runtime_smoke:
        lookback_days = int(getattr(cfg.validation.smoke, "lookback_days", 30))
        start = end - timedelta(days=lookback_days)
    else:
        start_text = cfg.validation.samples.is_research[0]
        start = _parse_utc_datetime(start_text)
    if start >= end:
        raise ValueError("validation start must be before end")
    return start, end


def _parse_utc_datetime(value: str) -> datetime:
    text = value.strip()
    if len(text) == 10:
        dt = datetime.fromisoformat(text).replace(tzinfo=UTC)
    else:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        dt = dt.astimezone(UTC)
    return dt


def _load_data(
    cfg: R3Config,
    symbols: list[str],
    start: datetime,
    end: datetime,
):
    cache_dir = _default_cache_dir()
    loader = R3DataLoader(cfg, cache_dir=cache_dir)
    exchange = R3ExchangeData(cfg, cache_dir=cache_dir)
    data_by_symbol = {}
    funding_by_symbol = {}
    premium_by_symbol = {}
    for symbol in symbols:
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
    return data_by_symbol, funding_by_symbol, premium_by_symbol


if __name__ == "__main__":
    raise SystemExit(main())

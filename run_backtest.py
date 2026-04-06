"""
TITAN v1 — 全市值前 20 大幣種回測腳本
執行方式：python run_backtest.py
"""

import sys
import time
from config.settings_loader import load_settings
from core.exchange import Exchange
from scanner.market_scanner import MarketScanner
from backtest.data_loader import DataLoader
from backtest.engine import BacktestEngine
from backtest.report import BacktestReport
from strategies.ema_crossover import EMAcrossover
from utils.logger import get_logger

logger = get_logger()

TIMEFRAME = "15m"
DAYS      = 30


def run():
    logger.info("=" * 60)
    logger.info("  TITAN v1 -- 全市值前 20 大幣種回測")
    logger.info(f"  週期：{TIMEFRAME} | 天數：{DAYS} 天")
    logger.info("=" * 60)

    # 1. 載入設定 + 連線
    settings = load_settings()
    exchange = Exchange(settings)
    exchange.connect()

    # 2. 取得前 20 大幣種
    scanner = MarketScanner(exchange, settings)
    symbols = scanner.get_tradeable_symbols()
    logger.info(f"[掃描] 取得 {len(symbols)} 個可交易幣種")

    # 3. 初始化策略
    strategy = EMAcrossover(settings)
    logger.info(f"[策略] {repr(strategy)}")

    # 4. 批量回測
    loader     = DataLoader(exchange)
    all_results = {}
    failed      = []

    for i, symbol in enumerate(symbols, 1):
        name = symbol.split("/")[0]
        logger.info(f"[{i:02d}/{len(symbols)}] 載入 {name}...")

        try:
            df = loader.fetch(symbol, TIMEFRAME, days=DAYS)
        except Exception as e:
            logger.warning(f"  [{name}] 載入失敗：{e}")
            failed.append(name)
            continue

        if len(df) < 120:
            logger.warning(f"  [{name}] K 線不足（{len(df)} 根），跳過")
            failed.append(name)
            continue

        try:
            engine  = BacktestEngine(strategy, settings)
            results = engine.run(df)
            results['symbol'] = name
            all_results[symbol] = results
            ret  = results['total_return_pct']
            sign = "+" if ret >= 0 else ""
            logger.info(
                f"  [{name}] {sign}{ret:.2f}% | "
                f"勝率 {results['win_rate_pct']:.1f}% | "
                f"{results['total_trades']} 筆"
            )
        except Exception as e:
            logger.warning(f"  [{name}] 回測失敗：{e}")
            failed.append(name)

        # 避免觸發 rate limit
        time.sleep(0.3)

    # 5. 彙總報告
    _print_summary(all_results, failed)


def _print_summary(all_results: dict, failed: list):
    if not all_results:
        print("\n[錯誤] 沒有任何幣種回測成功！")
        return

    results_list = list(all_results.values())

    # 各幣種統計
    total_trades_all  = sum(r['total_trades'] for r in results_list)
    total_wins        = sum(r['winning_trades'] for r in results_list)
    total_losses      = sum(r['losing_trades'] for r in results_list)
    avg_win_rate      = total_wins / total_trades_all * 100 if total_trades_all > 0 else 0
    avg_return        = sum(r['total_return_pct'] for r in results_list) / len(results_list)
    profitable_count  = sum(1 for r in results_list if r['total_return_pct'] > 0)

    # 每日平均開倉數（20 個幣種合計）
    avg_daily_trades  = total_trades_all / 30

    print("\n" + "=" * 62)
    print("  TITAN v1 — 前 20 大幣種 30 天回測彙總")
    print("=" * 62)
    print(f"  {'幣種':<8} {'報酬率':>8} {'勝率':>7} {'交易數':>6} {'最大回撤':>9} {'夏普':>7}")
    print("  " + "─" * 58)

    # 按報酬率排序
    sorted_results = sorted(results_list, key=lambda r: r['total_return_pct'], reverse=True)
    for r in sorted_results:
        ret  = r['total_return_pct']
        sign = "+" if ret >= 0 else ""
        mark = " <--" if ret > 0 else ""
        print(
            f"  {r['symbol']:<8} "
            f"{sign}{ret:>7.2f}% "
            f"{r['win_rate_pct']:>6.1f}% "
            f"{r['total_trades']:>6} "
            f"{r['max_drawdown_pct']:>8.2f}% "
            f"{r['sharpe_ratio']:>7.2f}"
            f"{mark}"
        )

    print("  " + "─" * 58)
    print(f"\n  [總計]")
    print(f"  成功回測：{len(results_list)} 個幣種" + (f"（{len(failed)} 個失敗：{', '.join(failed)}）" if failed else ""))
    print(f"  正向獲利：{profitable_count} / {len(results_list)} 個幣種")
    print(f"  30天總交易次數：{total_trades_all} 筆（平均每日 {avg_daily_trades:.1f} 筆）")
    print(f"  綜合勝率：{avg_win_rate:.1f}%（{total_wins} 勝 / {total_losses} 敗）")
    print(f"  平均報酬率：{'+'if avg_return>=0 else ''}{avg_return:.2f}%")
    print("=" * 62)

    # 每日開單分析
    print(f"\n  [開單頻率分析]")
    print(f"  30 天共 {total_trades_all} 筆 = 平均每日 {avg_daily_trades:.1f} 筆")
    print(f"  每日保證金需求（同時最多 3 倉）：${5000*0.10*3:,.0f} USDT")
    verdict = "OK" if avg_daily_trades >= 3 else "偏低，建議檢視濾網"
    print(f"  開單量評估：{verdict}")
    print("=" * 62)


if __name__ == "__main__":
    run()

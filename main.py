"""
TITAN v1 — 程式進入點
Trading Intelligent Tactical Automation Network

啟動方式：
  python main.py
"""

import sys
from dotenv import load_dotenv

from config.settings_loader import load_settings
from core.exchange import Exchange
from core.order_manager import OrderManager
from scanner.market_scanner import MarketScanner
from utils.logger import get_logger

load_dotenv()
logger = get_logger()


def main():
    logger.info("=" * 60)
    logger.info("  TITAN v1 啟動中...")
    logger.info("  Trading Intelligent Tactical Automation Network")
    logger.info("=" * 60)

    # 1. 載入設定
    logger.info("📋 載入設定檔...")
    try:
        settings = load_settings()
    except (FileNotFoundError, ValueError) as e:
        logger.error(f"設定檔錯誤：{e}")
        sys.exit(1)

    mode = settings.get("mode", "testnet")
    logger.info(f"🔧 交易模式：{'⚠️  正式網（真實資金）' if mode == 'live' else '🧪 測試網（假錢）'}")

    # 2. 連線交易所
    logger.info("🔌 連線至幣安...")
    try:
        exchange = Exchange(settings)
        exchange.connect()
    except (ValueError, ConnectionError) as e:
        logger.error(f"連線失敗：{e}")
        sys.exit(1)

    # 3. 顯示帳戶餘額
    try:
        balance = exchange.get_balance()
        total = exchange.get_total_balance()
        logger.info(f"💰 帳戶餘額：可用 ${balance:,.2f} USDT | 總計 ${total:,.2f} USDT")
    except Exception as e:
        logger.error(f"無法取得餘額：{e}")
        sys.exit(1)

    # 4. 掃描可交易幣種
    logger.info("🔍 掃描市值前 20 大幣種...")
    try:
        scanner = MarketScanner(exchange, settings)
        symbols = scanner.get_tradeable_symbols()
    except Exception as e:
        logger.error(f"幣種掃描失敗：{e}")
        sys.exit(1)

    # 5. 設定槓桿與保證金模式
    leverage = settings["risk"]["leverage"]
    margin_type = settings["risk"]["margin_type"]
    logger.info(f"⚙️  設定槓桿 {leverage}x、保證金模式：{'全倉' if margin_type == 'cross' else '逐倉'}")

    for symbol in symbols:
        try:
            exchange.set_leverage(symbol, leverage)
            exchange.set_margin_type(symbol, margin_type)
        except Exception as e:
            logger.warning(f"[{symbol}] 槓桿/保證金設定失敗（可能不支援）：{e}")

    # 6. Phase 1 驗證完成
    logger.info("=" * 60)
    logger.info("✅ Phase 1 驗證完成！")
    logger.info(f"   帳戶餘額：${balance:,.2f} USDT")
    logger.info(f"   可交易幣種：{len(symbols)} 個")
    logger.info(f"   槓桿：{leverage}x | 保證金模式：{'全倉' if margin_type == 'cross' else '逐倉'}")
    logger.info("   下一步：執行策略 + 回測（Phase 2）")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()

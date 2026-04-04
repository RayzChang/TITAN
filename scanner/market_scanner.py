"""
TITAN v1 — 市場掃描器（SAM 負責維護）
動態取得市值/交易量前 20 大幣種，每日更新一次
"""

from typing import Optional
from datetime import datetime, date

from core.exchange import Exchange
from scanner.symbol_filter import filter_symbols
from utils.logger import get_logger

logger = get_logger()


class MarketScanner:
    def __init__(self, exchange: Exchange, settings: dict):
        self.ex = exchange
        self.symbols_cfg = settings.get("symbols", {})
        self.top_n = 20

        self._cache: list[str] = []
        self._cache_date: Optional[date] = None

    def get_tradeable_symbols(self) -> list[str]:
        """取得今日可交易幣種清單（有快取則直接回傳）"""
        today = date.today()
        if self._cache and self._cache_date == today:
            return self._cache

        logger.info("🔍 正在掃描市值前 20 大幣種...")
        symbols = self._fetch_top_symbols()
        self._cache = symbols
        self._cache_date = today

        logger.info(f"✅ 今日可交易幣種（{len(symbols)} 個）：{', '.join(self._base_names(symbols))}")
        return symbols

    def _fetch_top_symbols(self) -> list[str]:
        """從幣安取得所有 USDT-M 合約，按 24h 交易量排序，取前 20"""
        try:
            tickers = self.ex.exchange.fetch_tickers()
        except Exception as e:
            logger.error(f"[掃描失敗] 無法取得行情列表：{e}")
            return self._fallback_symbols()

        exclude = self.symbols_cfg.get("exclude", [])
        all_valid = filter_symbols(list(tickers.keys()), exclude)

        # 按 24h 報價量排序（quoteVolume = USDT 交易量）
        scored = []
        for sym in all_valid:
            ticker = tickers.get(sym, {})
            vol = float(ticker.get("quoteVolume") or 0)
            scored.append((sym, vol))

        scored.sort(key=lambda x: x[1], reverse=True)
        top = [sym for sym, _ in scored[: self.top_n]]
        return top

    def _fallback_symbols(self) -> list[str]:
        """網路失敗時的備用清單（主流幣）"""
        logger.warning("⚠️  使用備用幣種清單")
        return [
            "BTC/USDT:USDT", "ETH/USDT:USDT", "BNB/USDT:USDT",
            "SOL/USDT:USDT", "XRP/USDT:USDT", "DOGE/USDT:USDT",
            "ADA/USDT:USDT", "AVAX/USDT:USDT", "LINK/USDT:USDT",
            "DOT/USDT:USDT",
        ]

    @staticmethod
    def _base_names(symbols: list[str]) -> list[str]:
        """從 BTC/USDT:USDT 提取 BTC"""
        return [s.split("/")[0] for s in symbols]

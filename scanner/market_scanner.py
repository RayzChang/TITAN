"""
TITAN v1 — 市場掃描器（SAM 負責維護）
取得「真實市值」前 20 大幣種，每日更新一次。

資料來源優先順序：
  1. CoinGecko 免費 API（market_cap_desc 排序）→ 交叉比對幣安有開合約的幣種
  2. 若 CoinGecko 失敗 → 改用幣安 24h 交易量排序（降級模式）
  3. 兩者都失敗 → 使用備用靜態清單
"""

import urllib.request
import json
from typing import Optional
from datetime import datetime, date

from core.exchange import Exchange
from utils.logger import get_logger

logger = get_logger()

# CoinGecko 免費 API（不需金鑰）
COINGECKO_URL = (
    "https://api.coingecko.com/api/v3/coins/markets"
    "?vs_currency=usd&order=market_cap_desc"
    "&per_page=100&page=1&sparkline=false"
)

# 幣安合約不支援、或回測表現太差需排除的幣種
HARDCODED_EXCLUDE = {
    "USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD",  # 穩定幣
    "WBTC", "WETH", "WBNB",                            # Wrapped 幣
}


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

        logger.info("[掃描] 正在取得市值前 20 大幣種...")
        symbols = self._fetch_top_symbols()
        self._cache = symbols
        self._cache_date = today

        logger.info(f"[掃描] 可交易幣種（{len(symbols)} 個）：{', '.join(self._base_names(symbols))}")
        return symbols

    # ------------------------------------------------------------------
    # 主流程：CoinGecko 市值排名 → 交叉比對 Binance 合約
    # ------------------------------------------------------------------

    def _fetch_top_symbols(self) -> list[str]:
        """整合 CoinGecko 市值排名 + Binance 合約可用性，回傳 ccxt 格式清單"""

        # Step 1：取得 Binance 上所有 USDT-M 合約（含 24h 量、價格）
        binance_map = self._fetch_binance_futures_map()

        # Step 2：取得 CoinGecko 市值排名
        cg_symbols = self._fetch_coingecko_market_cap()

        if cg_symbols:
            result = self._merge(cg_symbols, binance_map)
            if result:
                logger.info("[掃描] 資料來源：CoinGecko 市值排名")
                return result
            logger.warning("[掃描] CoinGecko 清單與 Binance 合約交叉比對結果不足，切換降級模式")
        else:
            logger.warning("[掃描] CoinGecko API 不可用，切換降級模式（Binance 交易量排序）")

        # Step 3 降級：純 Binance 交易量排序
        volume_result = self._fallback_by_volume(binance_map)
        if volume_result:
            logger.warning("[掃描] 資料來源：Binance 24h 交易量排序（降級）")
            return volume_result

        # Step 4：靜態備用清單
        return self._fallback_static()

    def _fetch_binance_futures_map(self) -> dict:
        """
        從 Binance fapiPublic 取得所有 USDT-M 合約的 24h 數據。
        回傳 dict: { 'BTC': {'ccxt_sym': 'BTC/USDT:USDT', 'volume': ..., 'price': ...} }
        """
        try:
            raw = self.ex.exchange.fapiPublicGetTicker24hr()
        except Exception as e:
            logger.error(f"[掃描] 無法取得 Binance 行情：{e}")
            return {}

        exclude_cfg = self.symbols_cfg.get("exclude", [])
        exclude_set = {s.upper() for s in exclude_cfg} | HARDCODED_EXCLUDE
        min_vol     = self.symbols_cfg.get("min_quote_volume_usdt", 10_000_000)   # 最低 1000 萬 USDT
        min_price   = self.symbols_cfg.get("min_price_usdt", 0.0001)              # 最低 $0.0001

        result = {}
        for t in raw:
            sym = t.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            base  = sym[:-4]
            price = float(t.get("lastPrice")   or 0)
            vol   = float(t.get("quoteVolume") or 0)
            if base in exclude_set or price < min_price or vol < min_vol:
                continue
            result[base.upper()] = {
                "ccxt_sym": f"{base}/USDT:USDT",
                "volume":   vol,
                "price":    price,
            }
        return result

    def _fetch_coingecko_market_cap(self) -> list[str]:
        """
        從 CoinGecko 取得市值前 100 名的幣種符號（大寫）。
        回傳 list，順序為市值由高到低，例如 ['BTC', 'ETH', 'BNB', ...]
        """
        try:
            req = urllib.request.Request(
                COINGECKO_URL,
                headers={"User-Agent": "TITAN/1.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            symbols = []
            for coin in data:
                sym = coin.get("symbol", "").upper()
                if sym:
                    symbols.append(sym)
            return symbols

        except Exception as e:
            logger.warning(f"[掃描] CoinGecko API 失敗：{e}")
            return []

    def _merge(self, cg_symbols: list[str], binance_map: dict) -> list[str]:
        """
        以 CoinGecko 市值排名為主，篩選 Binance 有開合約的幣種，
        取前 top_n 個。
        """
        result = []
        seen   = set()
        for sym in cg_symbols:
            if sym in binance_map and sym not in seen:
                result.append(binance_map[sym]["ccxt_sym"])
                seen.add(sym)
            if len(result) >= self.top_n:
                break
        return result

    def _fallback_by_volume(self, binance_map: dict) -> list[str]:
        """降級模式：用 Binance 24h 交易量排序取前 top_n"""
        if not binance_map:
            return []
        sorted_items = sorted(binance_map.values(), key=lambda x: x["volume"], reverse=True)
        return [item["ccxt_sym"] for item in sorted_items[: self.top_n]]

    def _fallback_static(self) -> list[str]:
        """網路完全失敗時的靜態備用清單（主流幣，2025 年市值前 20）"""
        logger.warning("[掃描] 使用靜態備用幣種清單")
        return [
            "BTC/USDT:USDT",  "ETH/USDT:USDT",  "BNB/USDT:USDT",
            "SOL/USDT:USDT",  "XRP/USDT:USDT",  "DOGE/USDT:USDT",
            "ADA/USDT:USDT",  "AVAX/USDT:USDT", "LINK/USDT:USDT",
            "DOT/USDT:USDT",  "TRX/USDT:USDT",  "MATIC/USDT:USDT",
            "LTC/USDT:USDT",  "UNI/USDT:USDT",  "ATOM/USDT:USDT",
            "BCH/USDT:USDT",  "NEAR/USDT:USDT", "OP/USDT:USDT",
            "ARB/USDT:USDT",  "FIL/USDT:USDT",
        ]

    @staticmethod
    def _base_names(symbols: list[str]) -> list[str]:
        """從 BTC/USDT:USDT 提取 BTC"""
        return [s.split("/")[0] for s in symbols]

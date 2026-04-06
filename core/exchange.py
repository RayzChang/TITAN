"""
TITAN v1 — 交易所連線模組（SHIELD 審核：風險隔離層）
封裝所有 ccxt API 呼叫，支援測試網/正式網切換，內建重試機制
"""

import os
import time
from typing import Optional

import ccxt
import pandas as pd
from dotenv import load_dotenv

from utils.logger import get_logger

load_dotenv()
logger = get_logger()


class Exchange:
    """幣安 USDT-M 合約交易所封裝"""

    MAX_RETRIES = 3
    RETRY_DELAYS = [1, 2, 4]  # 秒，指數退避

    def __init__(self, settings: dict):
        self.settings = settings
        self.is_testnet = settings.get("mode", "testnet") == "testnet"
        self.exchange: Optional[ccxt.binance] = None

    def connect(self):
        """建立交易所連線並驗證"""
        if self.is_testnet:
            api_key = os.getenv("BINANCE_TESTNET_API_KEY", "")
            api_secret = os.getenv("BINANCE_TESTNET_API_SECRET", "")
            mode_label = "測試網"
        else:
            api_key = os.getenv("BINANCE_API_KEY", "")
            api_secret = os.getenv("BINANCE_API_SECRET", "")
            mode_label = "正式網"

        if not api_key or not api_secret:
            raise ValueError(f"[連線失敗] 找不到 {mode_label} API 金鑰，請檢查 .env 檔案")

        params = {
            "apiKey": api_key,
            "secret": api_secret,
            "options": {
                "defaultType": "future",  # USDT-M 合約
                "recvWindow": 60000,
                "adjustForTimeDifference": True,
            },
        }

        # Demo Trading 模式：使用 demo-fapi.binance.com
        if self.is_testnet:
            params["urls"] = {
                "api": {
                    "fapiPublic":    "https://demo-fapi.binance.com/fapi/v1",
                    "fapiPrivate":   "https://demo-fapi.binance.com/fapi/v1",
                    "fapiPublicV2":  "https://demo-fapi.binance.com/fapi/v2",
                    "fapiPrivateV2": "https://demo-fapi.binance.com/fapi/v2",
                    "fapiPublicV3":  "https://demo-fapi.binance.com/fapi/v3",
                    "fapiPrivateV3": "https://demo-fapi.binance.com/fapi/v3",
                }
            }

        self.exchange = ccxt.binance(params)

        # Demo Trading：覆寫所有 fapi 端點到 demo-fapi.binance.com
        if self.is_testnet:
            demo_base = "https://demo-fapi.binance.com"
            for key in list(self.exchange.urls["api"].keys()):
                if key.startswith("fapi"):
                    version = "v2" if "V2" in key else ("v3" if "V3" in key else "v1")
                    self.exchange.urls["api"][key] = f"{demo_base}/fapi/{version}"

        # 載入市場資料（公開端點）
        try:
            self.exchange.load_markets()
        except Exception as e:
            logger.warning(f"[load_markets] {e}，嘗試繼續...")

        # 驗證 API 金鑰（私有端點）
        try:
            self.exchange.fapiPrivateV2GetAccount()
            logger.info(f"✅ 成功連線至幣安{mode_label}")
        except ccxt.AuthenticationError as e:
            raise ConnectionError(f"[連線失敗] API 金鑰驗證失敗，請確認 {mode_label} 金鑰是否正確：{e}")

    def get_balance(self) -> float:
        """取得帳戶 USDT 可用餘額"""
        def _fetch():
            account = self.exchange.fapiPrivateV2GetAccount()
            for asset in account.get("assets", []):
                if asset["asset"] == "USDT":
                    return float(asset["availableBalance"])
            return 0.0

        return self._retry(_fetch, "取得餘額")

    def get_total_balance(self) -> float:
        """取得帳戶 USDT 總餘額（含未實現損益）"""
        def _fetch():
            account = self.exchange.fapiPrivateV2GetAccount()
            for asset in account.get("assets", []):
                if asset["asset"] == "USDT":
                    return float(asset["walletBalance"])
            return 0.0

        return self._retry(_fetch, "取得總餘額")

    def get_ticker(self, symbol: str) -> dict:
        """取得當前報價"""
        return self._retry(
            lambda: self.exchange.fetch_ticker(symbol),
            f"取得報價 {symbol}"
        )

    def get_ohlcv(self, symbol: str, timeframe: str, limit: int = 100) -> pd.DataFrame:
        """取得 K 線數據，回傳 DataFrame（含 open/high/low/close/volume）"""
        def _fetch():
            raw = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            df.set_index("timestamp", inplace=True)
            return df

        return self._retry(_fetch, f"取得K線 {symbol} {timeframe}")

    def set_leverage(self, symbol: str, leverage: int):
        """設定槓桿倍數（直接呼叫 fapi，不走 sapi）"""
        ccxt_symbol = symbol.replace("/", "").replace(":USDT", "")  # BTC/USDT:USDT → BTCUSDT
        def _set():
            self.exchange.fapiPrivatePostLeverage({
                "symbol": ccxt_symbol,
                "leverage": leverage,
            })
            logger.info(f"槓桿設定：{symbol} = {leverage}x")

        self._retry(_set, f"設定槓桿 {symbol}")

    def set_margin_type(self, symbol: str, margin_type: str):
        """設定保證金模式（直接呼叫 fapi，不走 sapi）"""
        ccxt_symbol = symbol.replace("/", "").replace(":USDT", "")
        margin_upper = margin_type.upper()
        try:
            self.exchange.fapiPrivatePostMarginType({
                "symbol": ccxt_symbol,
                "marginType": margin_upper,   # CROSSED 或 ISOLATED
                "margintype": margin_upper,   # 部分版本用小寫 key
            })
            logger.info(f"保證金模式：{symbol} = {'全倉' if margin_upper == 'CROSS' else '逐倉'}")
        except ccxt.ExchangeError as e:
            err = str(e)
            # 已是相同模式、或 Demo Trading 不需要更改→忽略
            if "No need to change margin type" in err or "-4046" in err or "-1102" in err:
                pass
            else:
                raise

    def get_position(self, symbol: str) -> Optional[dict]:
        """取得指定幣種的持倉資訊，無持倉回傳 None"""
        def _fetch():
            positions = self.exchange.fetch_positions([symbol])
            for pos in positions:
                if float(pos.get("contracts", 0)) != 0:
                    return pos
            return None

        return self._retry(_fetch, f"取得持倉 {symbol}")

    def get_all_positions(self) -> list:
        """取得所有持倉"""
        def _fetch():
            positions = self.exchange.fetch_positions()
            return [p for p in positions if float(p.get("contracts", 0)) != 0]

        return self._retry(_fetch, "取得所有持倉")

    def get_open_orders(self, symbol: str) -> list:
        """取得指定幣種的所有掛單"""
        return self._retry(
            lambda: self.exchange.fetch_open_orders(symbol),
            f"取得掛單 {symbol}"
        )

    def cancel_all_orders(self, symbol: str):
        """取消指定幣種的所有掛單"""
        self._retry(
            lambda: self.exchange.cancel_all_orders(symbol),
            f"取消所有掛單 {symbol}"
        )

    def create_order(self, symbol: str, order_type: str, side: str,
                     amount: float, price: float = None, params: dict = None) -> dict:
        """建立訂單"""
        params = params or {}
        return self._retry(
            lambda: self.exchange.create_order(symbol, order_type, side, amount, price, params),
            f"建立訂單 {side} {symbol}"
        )

    def _retry(self, func, label: str):
        """通用重試機制，最多重試 MAX_RETRIES 次"""
        last_error = None
        for attempt, delay in enumerate(self.RETRY_DELAYS, 1):
            try:
                return func()
            except ccxt.NetworkError as e:
                last_error = e
                logger.warning(f"[{label}] 網路錯誤，{delay}秒後重試（{attempt}/{self.MAX_RETRIES}）：{e}")
                time.sleep(delay)
            except ccxt.RateLimitExceeded:
                logger.warning(f"[{label}] 請求頻率超限，等待 {delay * 2} 秒")
                time.sleep(delay * 2)
            except (ccxt.AuthenticationError, ccxt.InsufficientFunds, ccxt.InvalidOrder) as e:
                # 這類錯誤重試沒意義，直接拋出
                raise
            except ccxt.ExchangeError as e:
                last_error = e
                logger.warning(f"[{label}] 交易所錯誤（{attempt}/{self.MAX_RETRIES}）：{e}")
                time.sleep(delay)

        raise ConnectionError(f"[{label}] 重試 {self.MAX_RETRIES} 次後仍失敗：{last_error}")

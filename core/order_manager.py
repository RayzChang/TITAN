"""
TITAN v1 — 下單管理模組（SHIELD 審核：所有下單必須經過此模組）
負責開多、開空、平倉、止損止盈設定
"""

import math
from typing import Optional

import ccxt

from core.exchange import Exchange
from utils.logger import get_logger

logger = get_logger()


class OrderManager:
    def __init__(self, exchange: Exchange, settings: dict):
        self.ex = exchange
        self.risk = settings.get("risk", {})
        self.execution = settings.get("execution", {})

    def open_long(self, symbol: str, balance: float) -> Optional[dict]:
        """開多倉：市價買入 + 掛止損/止盈"""
        return self._open_position(symbol, balance, side="buy")

    def open_short(self, symbol: str, balance: float) -> Optional[dict]:
        """開空倉：市價賣出 + 掛止損/止盈"""
        return self._open_position(symbol, balance, side="sell")

    def close_position(self, symbol: str, position: dict) -> Optional[dict]:
        """市價平倉"""
        side = position.get("side", "")
        contracts = float(position.get("contracts", 0))
        if contracts == 0:
            return None

        close_side = "sell" if side == "long" else "buy"
        try:
            order = self.ex.create_order(
                symbol=symbol,
                order_type="market",
                side=close_side,
                amount=contracts,
                params={"reduceOnly": True},
            )
            logger.info(f"[平倉] {symbol} | 方向：{'多' if side == 'long' else '空'} | 數量：{contracts}")
            return order
        except ccxt.InsufficientFunds:
            logger.error(f"[平倉失敗] {symbol} 餘額不足")
            return None

    def cancel_all_orders(self, symbol: str):
        """取消指定幣種所有掛單"""
        try:
            self.ex.cancel_all_orders(symbol)
            logger.info(f"[取消掛單] {symbol} 所有掛單已取消")
        except Exception as e:
            logger.warning(f"[取消掛單] {symbol} 失敗：{e}")

    # ── 內部方法 ──────────────────────────────────────────────

    def _open_position(self, symbol: str, balance: float, side: str) -> Optional[dict]:
        """內部：建立倉位（市價 + 止損 + 止盈）"""
        ticker = self.ex.get_ticker(symbol)
        price = float(ticker["last"])

        amount = self._calc_amount(balance, price)
        if amount is None:
            return None

        sl_price, tp_price = self._calc_sl_tp(price, side)

        direction = "多" if side == "buy" else "空"
        logger.info(
            f"[開倉] {symbol} 開{direction} | 價格：{price:.4f} | "
            f"數量：{amount} | 止損：{sl_price:.4f} | 止盈：{tp_price:.4f}"
        )

        try:
            # 市價主單
            order = self.ex.create_order(
                symbol=symbol,
                order_type="market",
                side=side,
                amount=amount,
            )

            close_side = "sell" if side == "buy" else "buy"

            # 止損單（STOP_MARKET）
            self.ex.create_order(
                symbol=symbol,
                order_type="stop_market",
                side=close_side,
                amount=amount,
                params={
                    "stopPrice": sl_price,
                    "reduceOnly": True,
                    "closePosition": False,
                },
            )

            # 止盈單（TAKE_PROFIT_MARKET）
            self.ex.create_order(
                symbol=symbol,
                order_type="take_profit_market",
                side=close_side,
                amount=amount,
                params={
                    "stopPrice": tp_price,
                    "reduceOnly": True,
                    "closePosition": False,
                },
            )

            logger.info(f"[開倉成功] {symbol} 開{direction} 訂單已送出")
            return order

        except ccxt.InsufficientFunds:
            logger.error(f"[開倉失敗] {symbol} 餘額不足，跳過此筆交易")
            return None
        except ccxt.InvalidOrder as e:
            logger.error(f"[開倉失敗] {symbol} 訂單格式錯誤：{e}")
            return None

    def _calc_amount(self, balance: float, price: float) -> Optional[float]:
        """計算下單數量（根據倉位比例和槓桿）"""
        pos_pct = self.risk.get("position_size_pct", 10) / 100
        leverage = self.risk.get("leverage", 20)

        margin = balance * pos_pct          # 單筆保證金
        notional = margin * leverage        # 槓桿後名義價值
        amount = notional / price           # 換算成幣種數量

        # 取得交易所精度，四捨五入到合規數量
        try:
            market = self.ex.exchange.market(symbol=None)
        except Exception:
            market = None

        if amount <= 0:
            logger.warning(f"[下單] 計算數量為零，餘額：{balance:.2f}，價格：{price:.4f}")
            return None

        # 簡單取 4 位小數（正式使用時可從 market info 取精度）
        amount = math.floor(amount * 1000) / 1000
        return amount if amount > 0 else None

    def _calc_sl_tp(self, price: float, side: str) -> tuple[float, float]:
        """計算止損和止盈價格"""
        sl_pct = self.risk.get("stop_loss_pct", 1.5) / 100
        tp_pct = self.risk.get("take_profit_pct", 3.0) / 100

        if side == "buy":   # 做多
            sl = price * (1 - sl_pct)
            tp = price * (1 + tp_pct)
        else:               # 做空
            sl = price * (1 + sl_pct)
            tp = price * (1 - tp_pct)

        return round(sl, 4), round(tp, 4)

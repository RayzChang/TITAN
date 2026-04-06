"""
策略抽象基類 (Base Strategy Abstract Class)

所有交易策略必須繼承此類並實作三個抽象方法：
  - calculate_signals : 根據 OHLCV DataFrame 回傳交易訊號
  - get_stop_loss     : 根據入場價與方向計算止損價位
  - get_take_profit   : 根據入場價與方向計算止盈價位
"""
from abc import ABC, abstractmethod
import pandas as pd


class BaseStrategy(ABC):
    """
    TITAN 策略抽象基類。

    子類別實作規範
    --------------
    - calculate_signals 必須回傳以下三個字串之一：'LONG' / 'SHORT' / 'HOLD'
    - get_stop_loss / get_take_profit 均接受 float 型態的 entry_price 以及
      字串型態的 signal，並回傳 float 型態的價位。
    """

    @abstractmethod
    def calculate_signals(self, df: pd.DataFrame) -> str:
        """
        根據 OHLCV DataFrame 計算當前交易訊號。

        Parameters
        ----------
        df : pd.DataFrame
            含 open / high / low / close / volume 欄位的 K 線資料。

        Returns
        -------
        str
            'LONG'  — 做多
            'SHORT' — 做空
            'HOLD'  — 觀望，不操作
        """
        pass

    @abstractmethod
    def get_stop_loss(self, entry_price: float, signal: str) -> float:
        """
        根據入場價與訊號方向計算止損價位。

        Parameters
        ----------
        entry_price : float  入場價
        signal      : str    'LONG' 或 'SHORT'

        Returns
        -------
        float  止損價位
        """
        pass

    @abstractmethod
    def get_take_profit(self, entry_price: float, signal: str) -> float:
        """
        根據入場價與訊號方向計算止盈價位。

        Parameters
        ----------
        entry_price : float  入場價
        signal      : str    'LONG' 或 'SHORT'

        Returns
        -------
        float  止盈價位
        """
        pass

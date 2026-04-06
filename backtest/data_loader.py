"""
TITAN v1 — 回測資料載入模組
功能：從幣安 Demo Trading 取得歷史 K 線，快取到 CSV 避免重複下載
"""

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd


# interval 字串對應表（timeframe → binance interval）
INTERVAL_MAP = {
    '1m':  '1m',
    '5m':  '5m',
    '15m': '15m',
    '1h':  '1h',
    '4h':  '4h',
}

# 每個 timeframe 一根 K 線對應幾秒
TIMEFRAME_SECONDS = {
    '1m':  60,
    '5m':  300,
    '15m': 900,
    '1h':  3600,
    '4h':  14400,
}

# 資料快取目錄
DATA_DIR = Path('D:/02_trading/data')
MAX_PER_REQUEST = 1500  # 幣安每次最多回傳 1500 根


class DataLoader:
    """從幣安拉取歷史 K 線，支援 CSV 快取"""

    def __init__(self, exchange):
        """
        Parameters
        ----------
        exchange : core.exchange.Exchange 實例
            需已呼叫 connect()
        """
        self.exchange = exchange
        DATA_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 公開介面
    # ------------------------------------------------------------------

    def fetch(self, symbol: str, timeframe: str, days: int = 30) -> pd.DataFrame:
        """
        取得歷史 OHLCV，先查 CSV 快取再從 API 拉。

        Parameters
        ----------
        symbol    : 交易對，e.g. 'BTC/USDT:USDT' 或 'BTCUSDT'
        timeframe : K 線週期，e.g. '15m'
        days      : 往前取幾天的資料（預設 30）

        Returns
        -------
        pd.DataFrame
            index=timestamp(DatetimeIndex, UTC)
            columns=[open, high, low, close, volume]，皆為 float64
        """
        cache_path = self._cache_path(symbol, timeframe)
        since_ms = self._since_ms(days)

        # 嘗試讀取快取
        cached_df = self._load_cache(cache_path, since_ms)
        if cached_df is not None:
            return cached_df

        # 沒有快取 → 從 API 拉取
        df = self._fetch_from_api(symbol, timeframe, since_ms)

        # 儲存快取
        df.to_csv(cache_path)

        return df

    def _cache_path(self, symbol: str, timeframe: str) -> Path:
        """
        回傳快取檔案路徑，e.g. data/BTC_USDT_15m.csv
        支援 'BTC/USDT:USDT'、'BTCUSDT' 等格式
        """
        clean = symbol.replace('/', '_').replace(':', '_').replace('-', '_')
        filename = f"{clean}_{timeframe}.csv"
        return DATA_DIR / filename

    # ------------------------------------------------------------------
    # 內部方法
    # ------------------------------------------------------------------

    def _since_ms(self, days: int) -> int:
        """回傳 N 天前的 Unix timestamp（毫秒）"""
        since = datetime.now(timezone.utc) - timedelta(days=days)
        return int(since.timestamp() * 1000)

    def _load_cache(self, cache_path: Path, since_ms: int) -> pd.DataFrame | None:
        """
        若快取存在且資料夠新（最後一根 K 線距今 < 2 個 K 線週期），回傳 DataFrame；
        否則回傳 None。
        """
        if not cache_path.exists():
            return None

        try:
            df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
            if df.empty:
                return None

            # 確保 index 是 UTC DatetimeIndex
            if df.index.tz is None:
                df.index = df.index.tz_localize('UTC')

            # 確保 since 有 tz
            since_dt = datetime.fromtimestamp(since_ms / 1000, tz=timezone.utc)

            # 檢查快取是否覆蓋所需時間範圍
            if df.index[0] > since_dt:
                return None  # 快取起點太晚，需要重新拉

            # 確認欄位完整
            for col in ['open', 'high', 'low', 'close', 'volume']:
                if col not in df.columns:
                    return None

            df = df[['open', 'high', 'low', 'close', 'volume']].astype(float)
            return df

        except Exception:
            return None

    def _fetch_from_api(self, symbol: str, timeframe: str, since_ms: int) -> pd.DataFrame:
        """
        分批從幣安拉取 K 線，組合成完整 DataFrame。
        使用 fapiPublicGetKlines 底層 API（繞過 Demo Trading 路由問題）。
        """
        interval = INTERVAL_MAP.get(timeframe, timeframe)
        bar_symbol = self._to_binance_symbol(symbol)
        tf_sec = TIMEFRAME_SECONDS.get(timeframe, 900)

        all_bars: list[list] = []
        current_since = since_ms
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

        while current_since < now_ms:
            params = {
                'symbol':    bar_symbol,
                'interval':  interval,
                'startTime': current_since,
                'limit':     MAX_PER_REQUEST,
            }
            raw = self.exchange.exchange.fapiPublicGetKlines(params)

            if not raw:
                break

            all_bars.extend(raw)

            # 下一批從最後一根 K 線的下一個週期開始
            last_open_ms = int(raw[-1][0])
            current_since = last_open_ms + tf_sec * 1000

            # 避免打過頭或無限迴圈
            if len(raw) < MAX_PER_REQUEST:
                break

            time.sleep(0.2)  # 避免觸發 rate limit

        if not all_bars:
            return pd.DataFrame(columns=['open', 'high', 'low', 'close', 'volume'])

        df = self._parse_raw(all_bars)
        return df

    def _parse_raw(self, raw: list) -> pd.DataFrame:
        """
        將幣安 K 線原始資料解析成 DataFrame。
        raw 格式：[open_time, open, high, low, close, volume, ...]
        """
        records = []
        for bar in raw:
            records.append({
                'timestamp': int(bar[0]),
                'open':      float(bar[1]),
                'high':      float(bar[2]),
                'low':       float(bar[3]),
                'close':     float(bar[4]),
                'volume':    float(bar[5]),
            })

        df = pd.DataFrame(records)
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
        df.set_index('timestamp', inplace=True)
        df = df[['open', 'high', 'low', 'close', 'volume']].astype(float)
        df = df[~df.index.duplicated(keep='last')]
        df.sort_index(inplace=True)
        return df

    @staticmethod
    def _to_binance_symbol(symbol: str) -> str:
        """
        統一轉換成幣安合約格式，e.g.:
          'BTC/USDT:USDT' → 'BTCUSDT'
          'BTC/USDT'       → 'BTCUSDT'
          'BTCUSDT'        → 'BTCUSDT'
        """
        s = symbol.split(':')[0]        # 去掉 ':USDT'
        s = s.replace('/', '')          # 去掉 '/'
        return s.upper()

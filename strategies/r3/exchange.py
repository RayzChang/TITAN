"""
R3 Exchange Data API
====================

Spec   : docs/R3_spec.md §6, §9
Config : config/r3_strategy.yaml `funding`, `regime.c_extreme`

職責
----
- 抓取**幣安永續**的非 OHLCV 衍生資料：
    1. funding rate 歷史
    2. mark price klines
    3. index price klines
    4. premium index klines

- 統一 DF 格式（DatetimeIndex(UTC) + 標準欄位）
- 自動 pagination
- 缺資料 / API 限制 → 寫進 `missing_data_report.md`，**不假造資料**

注意
----
- 這個模組**只讀取公開資料**，不需要 API key（不同於 `core/exchange.py`）
- 不下單、不改持倉
- 任何 API 失敗都會明確記錄並回傳空 DF / NaN，後續流程能感知資料不足
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ccxt
import pandas as pd

from .config_loader import R3Config
from .data_loader import (
    TIMEFRAME_TO_SECONDS,
    RATE_LIMIT_SLEEP_SEC,
    _default_cache_dir,
    _symbol_to_filename,
    make_public_client,
)


# 幣安 API 限制
FUNDING_RATE_MAX_PER_CALL = 1000
PREMIUM_INDEX_KLINES_MAX_PER_CALL = 1500
MARK_INDEX_KLINES_MAX_PER_CALL = 1500


# -----------------------------------------------------------------------------
# Symbol conversion
# -----------------------------------------------------------------------------
def _symbol_to_native(symbol: str) -> str:
    """`BTC/USDT:USDT` → `BTCUSDT`（給 fapi 直接呼叫用）。"""
    return _symbol_to_filename(symbol)


# -----------------------------------------------------------------------------
# Cache helpers
# -----------------------------------------------------------------------------
def _funding_cache_path(cache_dir: Path, symbol: str) -> Path:
    sym_dir = cache_dir / _symbol_to_filename(symbol)
    sym_dir.mkdir(parents=True, exist_ok=True)
    return sym_dir / "funding.csv"


def _premium_cache_path(cache_dir: Path, symbol: str, timeframe: str) -> Path:
    sym_dir = cache_dir / _symbol_to_filename(symbol)
    sym_dir.mkdir(parents=True, exist_ok=True)
    return sym_dir / f"premium_{timeframe}.csv"


def _mark_cache_path(cache_dir: Path, symbol: str, timeframe: str) -> Path:
    sym_dir = cache_dir / _symbol_to_filename(symbol)
    sym_dir.mkdir(parents=True, exist_ok=True)
    return sym_dir / f"mark_{timeframe}.csv"


def _index_cache_path(cache_dir: Path, symbol: str, timeframe: str) -> Path:
    sym_dir = cache_dir / _symbol_to_filename(symbol)
    sym_dir.mkdir(parents=True, exist_ok=True)
    return sym_dir / f"index_{timeframe}.csv"


# -----------------------------------------------------------------------------
# R3ExchangeData
# -----------------------------------------------------------------------------
class R3ExchangeData:
    """公開資料 API 包裝（funding / mark / index / premium）。"""

    FUNDING_COLS = ["funding_rate", "mark_price"]
    PRICE_KLINES_COLS = ["open", "high", "low", "close"]
    PREMIUM_KLINES_COLS = ["open", "high", "low", "close"]

    def __init__(
        self,
        config: R3Config,
        cache_dir: Path | None = None,
        client: "ccxt.Exchange | None" = None,
    ):
        self.config = config
        self.cache_dir = Path(cache_dir) if cache_dir else _default_cache_dir()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._client = client
        self._api_limits: list[str] = []

    @property
    def client(self) -> "ccxt.Exchange":
        if self._client is None:
            self._client = make_public_client()
        return self._client

    @property
    def api_limits(self) -> list[str]:
        return list(self._api_limits)

    # =========================================================================
    # Funding rate history
    # =========================================================================
    def fetch_funding_history(
        self,
        symbol: str,
        start: datetime | None = None,
        end: datetime | None = None,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        """
        回傳 funding rate 歷史。

        欄位
        ----
        - index : DatetimeIndex(UTC)，funding 結算時點
        - funding_rate : float（小數，**非百分比**；e.g. 0.0001 = 0.01%）
        - mark_price : float（API 隨附）

        資料不足時：
        - 寫 `api_limits`
        - 回傳空 DF（caller 應自行判斷）
        """
        end = end or datetime.now(timezone.utc)
        start = start or (end - timedelta(days=self.config.funding.lookback_days))
        self._validate_utc(start, end)

        cache_df = pd.DataFrame() if force_refresh else self._read_funding_cache(symbol)
        missing = self._missing_ranges(cache_df, start, end)

        fetched: list[pd.DataFrame] = [cache_df] if not cache_df.empty else []
        for r_start, r_end in missing:
            df_part = self._fetch_funding_paginated(symbol, r_start, r_end)
            if not df_part.empty:
                fetched.append(df_part)

        if fetched:
            merged = pd.concat(fetched).sort_index()
            merged = merged[~merged.index.duplicated(keep="last")]
        else:
            merged = pd.DataFrame(columns=self.FUNDING_COLS).astype(float)

        if not merged.empty:
            self._write_funding_cache(symbol, merged)

        if merged.empty:
            return merged
        return merged.loc[(merged.index >= start) & (merged.index <= end)]

    def _fetch_funding_paginated(
        self, symbol: str, start: datetime, end: datetime,
    ) -> pd.DataFrame:
        if start >= end:
            return pd.DataFrame(columns=self.FUNDING_COLS).astype(float)

        native = _symbol_to_native(symbol)
        since_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)

        all_rows: list[dict] = []
        cursor = since_ms

        while cursor < end_ms:
            params = {
                "symbol": native,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": FUNDING_RATE_MAX_PER_CALL,
            }
            try:
                rows = self.client.fapiPublicGetFundingRate(params)
            except ccxt.BaseError as e:
                self._api_limits.append(
                    f"`{symbol}` fetch_funding failed at since={cursor}: "
                    f"{type(e).__name__}: {e}"
                )
                break

            if not rows:
                break

            all_rows.extend(rows)
            last_t = int(rows[-1]["fundingTime"])
            next_cursor = last_t + 1

            # 已涵蓋到 end_ms → 完成
            if next_cursor >= end_ms:
                break

            if next_cursor <= cursor:
                self._api_limits.append(
                    f"`{symbol}` funding pagination stalled at {cursor}"
                )
                break
            cursor = next_cursor

            # 不依賴 len(rows) == limit 判斷結束（ccxt 內部可能 cap）
            time.sleep(RATE_LIMIT_SLEEP_SEC)

        if not all_rows:
            return pd.DataFrame(columns=self.FUNDING_COLS).astype(float)

        df = pd.DataFrame(all_rows)
        # ccxt 從幣安回傳的 fundingTime 是字串 → 先轉 int 再 to_datetime
        df["timestamp"] = pd.to_datetime(
            pd.to_numeric(df["fundingTime"]), unit="ms", utc=True,
        )
        df["funding_rate"] = pd.to_numeric(df["fundingRate"]).astype(float)
        df["mark_price"] = pd.to_numeric(df["markPrice"]).astype(float)
        df = df.set_index("timestamp")[self.FUNDING_COLS]
        df = df[~df.index.duplicated(keep="last")].sort_index()
        return df

    # =========================================================================
    # Mark price klines
    # =========================================================================
    def fetch_mark_price_klines(
        self,
        symbol: str,
        timeframe: str,
        start: datetime | None = None,
        end: datetime | None = None,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        """
        Mark price OHLC（依 timeframe 對齊）。

        Note: Binance `markPriceKlines` 不回傳 volume（mark price 是計算出來的）。
        """
        return self._fetch_price_klines_generic(
            symbol, timeframe, start, end,
            endpoint_attr="fapiPublicGetMarkPriceKlines",
            cache_path_fn=lambda: _mark_cache_path(self.cache_dir, symbol, timeframe),
            force_refresh=force_refresh,
            kind="mark",
        )

    # =========================================================================
    # Index price klines
    # =========================================================================
    def fetch_index_price_klines(
        self,
        symbol: str,
        timeframe: str,
        start: datetime | None = None,
        end: datetime | None = None,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        """Index price OHLC。Binance fapi/v1/indexPriceKlines。"""
        # Note: 幣安 indexPriceKlines 用 `pair` 而不是 `symbol`，但 ccxt 已抽象掉
        # 公司會傳 BTCUSDT 即可
        return self._fetch_price_klines_generic(
            symbol, timeframe, start, end,
            endpoint_attr="fapiPublicGetIndexPriceKlines",
            cache_path_fn=lambda: _index_cache_path(self.cache_dir, symbol, timeframe),
            force_refresh=force_refresh,
            kind="index",
            symbol_param_key="pair",
        )

    # =========================================================================
    # Premium index klines
    # =========================================================================
    def fetch_premium_index_klines(
        self,
        symbol: str,
        timeframe: str,
        start: datetime | None = None,
        end: datetime | None = None,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        """
        Premium index OHLC（永續溢價率）。

        欄位 `close` 即為該 bar 結束時點的 `(mark - index) / index` 比率。
        用於 premium_z 計算。
        """
        return self._fetch_price_klines_generic(
            symbol, timeframe, start, end,
            endpoint_attr="fapiPublicGetPremiumIndexKlines",
            cache_path_fn=lambda: _premium_cache_path(self.cache_dir, symbol, timeframe),
            force_refresh=force_refresh,
            kind="premium",
        )

    # =========================================================================
    # Generic price-klines fetcher
    # =========================================================================
    def _fetch_price_klines_generic(
        self,
        symbol: str,
        timeframe: str,
        start: datetime | None,
        end: datetime | None,
        endpoint_attr: str,
        cache_path_fn,
        force_refresh: bool,
        kind: str,
        symbol_param_key: str = "symbol",
    ) -> pd.DataFrame:
        if timeframe not in TIMEFRAME_TO_SECONDS:
            raise ValueError(f"Unsupported timeframe: {timeframe}")

        end = end or datetime.now(timezone.utc)
        start = start or (end - timedelta(days=90))
        self._validate_utc(start, end)

        cache_path = cache_path_fn()
        cache_df = pd.DataFrame() if force_refresh else self._read_klines_cache(cache_path)
        missing = self._missing_ranges(cache_df, start, end)

        fetched: list[pd.DataFrame] = [cache_df] if not cache_df.empty else []
        for r_start, r_end in missing:
            df_part = self._fetch_klines_paginated(
                symbol, timeframe, r_start, r_end,
                endpoint_attr=endpoint_attr,
                kind=kind,
                symbol_param_key=symbol_param_key,
            )
            if not df_part.empty:
                fetched.append(df_part)

        if fetched:
            merged = pd.concat(fetched).sort_index()
            merged = merged[~merged.index.duplicated(keep="last")]
        else:
            merged = pd.DataFrame(columns=self.PRICE_KLINES_COLS).astype(float)

        if not merged.empty:
            self._write_klines_cache(cache_path, merged)

        if merged.empty:
            return merged
        return merged.loc[(merged.index >= start) & (merged.index <= end)]

    def _fetch_klines_paginated(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        endpoint_attr: str,
        kind: str,
        symbol_param_key: str,
    ) -> pd.DataFrame:
        if start >= end:
            return pd.DataFrame(columns=self.PRICE_KLINES_COLS).astype(float)

        native = _symbol_to_native(symbol)
        tf_sec = TIMEFRAME_TO_SECONDS[timeframe]
        since_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)

        endpoint = getattr(self.client, endpoint_attr, None)
        if endpoint is None:
            self._api_limits.append(
                f"ccxt does not expose `{endpoint_attr}` (kind={kind}); "
                "可能需升級 ccxt 版本"
            )
            return pd.DataFrame(columns=self.PRICE_KLINES_COLS).astype(float)

        all_rows: list[list] = []
        cursor = since_ms

        while cursor < end_ms:
            params = {
                symbol_param_key: native,
                "interval": timeframe,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": PREMIUM_INDEX_KLINES_MAX_PER_CALL,
            }
            try:
                rows = endpoint(params)
            except ccxt.BaseError as e:
                self._api_limits.append(
                    f"`{symbol}` `{timeframe}` {kind} klines failed at "
                    f"since={cursor}: {type(e).__name__}: {e}"
                )
                break

            if not rows:
                break

            rows = [r for r in rows if int(r[0]) <= end_ms]
            if not rows:
                break

            all_rows.extend(rows)
            last_open_ms = int(rows[-1][0])
            next_cursor = last_open_ms + tf_sec * 1000

            # 已涵蓋到 end_ms → 完成
            if next_cursor >= end_ms:
                break

            if next_cursor <= cursor:
                self._api_limits.append(
                    f"`{symbol}` `{timeframe}` {kind} pagination stalled at {cursor}"
                )
                break
            cursor = next_cursor

            # 不依賴 len(rows) == limit 判斷結束
            time.sleep(RATE_LIMIT_SLEEP_SEC)

        if not all_rows:
            return pd.DataFrame(columns=self.PRICE_KLINES_COLS).astype(float)

        # rows: [open_time, o, h, l, c, ...]
        records = []
        for r in all_rows:
            records.append({
                "timestamp": int(r[0]),
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
            })
        df = pd.DataFrame(records)
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df = df.set_index("timestamp")[self.PRICE_KLINES_COLS]
        df = df[~df.index.duplicated(keep="last")].sort_index()
        return df

    # =========================================================================
    # Cache helpers
    # =========================================================================
    def _read_funding_cache(self, symbol: str) -> pd.DataFrame:
        path = _funding_cache_path(self.cache_dir, symbol)
        if not path.exists():
            return pd.DataFrame(columns=self.FUNDING_COLS).astype(float)
        try:
            df = pd.read_csv(path, index_col=0)
            df.index = pd.to_datetime(df.index, utc=True, format="ISO8601")
            df = df[self.FUNDING_COLS].astype(float)
            df = df[~df.index.duplicated(keep="last")].sort_index()
            return df
        except Exception as e:
            self._api_limits.append(f"funding cache read failed for `{symbol}`: {e}")
            return pd.DataFrame(columns=self.FUNDING_COLS).astype(float)

    def _write_funding_cache(self, symbol: str, df: pd.DataFrame) -> None:
        path = _funding_cache_path(self.cache_dir, symbol)
        df_to_write = df[self.FUNDING_COLS].copy()
        df_to_write.index.name = "timestamp"
        df_to_write.to_csv(path)

    def _read_klines_cache(self, path: Path) -> pd.DataFrame:
        if not path.exists():
            return pd.DataFrame(columns=self.PRICE_KLINES_COLS).astype(float)
        try:
            df = pd.read_csv(path, index_col=0)
            df.index = pd.to_datetime(df.index, utc=True, format="ISO8601")
            df = df[self.PRICE_KLINES_COLS].astype(float)
            df = df[~df.index.duplicated(keep="last")].sort_index()
            return df
        except Exception as e:
            self._api_limits.append(f"klines cache read failed `{path.name}`: {e}")
            return pd.DataFrame(columns=self.PRICE_KLINES_COLS).astype(float)

    def _write_klines_cache(self, path: Path, df: pd.DataFrame) -> None:
        df_to_write = df[self.PRICE_KLINES_COLS].copy()
        df_to_write.index.name = "timestamp"
        df_to_write.to_csv(path)

    # =========================================================================
    # Helpers
    # =========================================================================
    @staticmethod
    def _validate_utc(start: datetime, end: datetime) -> None:
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("start / end must be timezone-aware (UTC)")

    @staticmethod
    def _missing_ranges(
        cache_df: pd.DataFrame,
        start: datetime,
        end: datetime,
    ) -> list[tuple[datetime, datetime]]:
        if cache_df.empty:
            return [(start, end)]
        cache_start = cache_df.index.min().to_pydatetime()
        cache_end = cache_df.index.max().to_pydatetime()
        missing: list[tuple[datetime, datetime]] = []
        if start < cache_start:
            missing.append((start, cache_start))
        if end > cache_end:
            missing.append((cache_end, end))
        return missing

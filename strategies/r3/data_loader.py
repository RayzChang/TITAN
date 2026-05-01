"""
R3 Data Loader — 5m / 1h / 4h K bar fetching with cache + integrity checks
==========================================================================

Spec   : docs/R3_spec.md §2 (時間週期)
Config : config/r3_strategy.yaml `universe`, `timeframes`

職責
----
- 從幣安永續 (`binanceusdm`) 公開端點抓取 OHLCV
- 自動 pagination（單次最多 1500 根）
- CSV cache（路徑 `data/r3_cache/<SYMBOL>/<timeframe>.csv`）
- 資料完整性檢查（缺 K / 重複 timestamp / null OHLCV / 排序 / time gap）
- 缺漏寫入 `data/r3_cache/missing_data_report.md`，**禁止假資料 / 禁止 forward fill**

工程紀律
--------
- 所有 magic number 從 `R3Config` 取，禁止 hardcode
- 公開 API 不需要 API key，用乾淨的 `ccxt.binanceusdm()` instance
- 任何資料缺失或 API 限制 → 寫報告，回傳 `None` 或空 DF，**不假裝成功**
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import ccxt
import pandas as pd

from .config_loader import R3Config


# -----------------------------------------------------------------------------
# 常數（屬於 ccxt / 幣安 API 限制，非交易策略參數，故不放 yaml）
# -----------------------------------------------------------------------------
BINANCE_OHLCV_MAX_PER_CALL = 1500       # 幣安 fapi /klines 單次上限
RATE_LIMIT_SLEEP_SEC = 0.2              # 連續 pagination 之間的 sleep
TIMEFRAME_TO_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400,
}


# -----------------------------------------------------------------------------
# Path helpers
# -----------------------------------------------------------------------------
def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_cache_dir() -> Path:
    return _project_root() / "data" / "r3_cache"


def _missing_report_path(cache_dir: Path) -> Path:
    return cache_dir / "missing_data_report.md"


def _symbol_to_filename(symbol: str) -> str:
    """`BTC/USDT:USDT` → `BTCUSDT`（ccxt unified → binance native）。"""
    s = symbol.split(":")[0].replace("/", "")
    return s.upper()


# -----------------------------------------------------------------------------
# Integrity report
# -----------------------------------------------------------------------------
@dataclass
class IntegrityReport:
    symbol: str
    timeframe: str
    n_bars: int
    n_duplicates: int
    n_nulls: int
    is_sorted: bool
    expected_interval_sec: int
    n_gaps: int
    gap_intervals: list[tuple[pd.Timestamp, pd.Timestamp]] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return (
            self.n_duplicates == 0
            and self.n_nulls == 0
            and self.is_sorted
            and self.n_gaps == 0
        )

    def issues_summary(self) -> list[str]:
        out: list[str] = []
        if self.n_duplicates > 0:
            out.append(f"{self.n_duplicates} duplicated timestamps")
        if self.n_nulls > 0:
            out.append(f"{self.n_nulls} rows with null OHLCV")
        if not self.is_sorted:
            out.append("timestamps are not monotonic increasing")
        if self.n_gaps > 0:
            out.append(f"{self.n_gaps} time gaps (expected interval {self.expected_interval_sec}s)")
        return out


def check_integrity(df: pd.DataFrame, symbol: str, timeframe: str) -> IntegrityReport:
    """純函數：檢查 DF 完整性，不修改資料。"""
    n_bars = len(df)
    expected_sec = TIMEFRAME_TO_SECONDS[timeframe]

    if n_bars == 0:
        return IntegrityReport(
            symbol=symbol, timeframe=timeframe, n_bars=0,
            n_duplicates=0, n_nulls=0, is_sorted=True,
            expected_interval_sec=expected_sec, n_gaps=0,
        )

    n_duplicates = int(df.index.duplicated().sum())

    ohlcv_cols = ["open", "high", "low", "close", "volume"]
    n_nulls = int(df[ohlcv_cols].isna().any(axis=1).sum())

    is_sorted = df.index.is_monotonic_increasing

    # Gap detection — 連續兩根 K 間距必須等於 expected_sec
    gaps: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    if n_bars > 1 and is_sorted:
        diffs = df.index.to_series().diff().dt.total_seconds()
        gap_mask = (diffs > expected_sec).fillna(False)
        prev_index = df.index[:-1]
        curr_index = df.index[1:]
        for prev_t, curr_t, is_gap in zip(prev_index, curr_index, gap_mask.iloc[1:]):
            if is_gap:
                gaps.append((prev_t, curr_t))

    return IntegrityReport(
        symbol=symbol,
        timeframe=timeframe,
        n_bars=n_bars,
        n_duplicates=n_duplicates,
        n_nulls=n_nulls,
        is_sorted=is_sorted,
        expected_interval_sec=expected_sec,
        n_gaps=len(gaps),
        gap_intervals=gaps,
    )


def write_missing_data_report(
    reports: Iterable[IntegrityReport],
    api_limits: list[str],
    cache_dir: Path,
) -> Path | None:
    """若任一 report 不乾淨或有 API 限制，寫報告。回傳路徑或 None（無問題）。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    report_path = _missing_report_path(cache_dir)

    problem_reports = [r for r in reports if not r.is_clean]

    if not problem_reports and not api_limits:
        # 無問題 → 移除舊報告（避免誤導）
        if report_path.exists():
            report_path.unlink()
        return None

    lines: list[str] = []
    lines.append("# R3 Missing Data Report")
    lines.append("")
    lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    lines.append("")
    lines.append("> 此報告自動產生。R3 不會用假資料補洞，以下狀況需 BOSS / MIA 評估：")
    lines.append("")

    if api_limits:
        lines.append("## API / 資料來源限制")
        lines.append("")
        for note in api_limits:
            lines.append(f"- {note}")
        lines.append("")

    if problem_reports:
        lines.append("## 資料完整性問題")
        lines.append("")
        lines.append("| Symbol | Timeframe | bars | duplicates | nulls | sorted | gaps |")
        lines.append("|---|---|---:|---:|---:|---|---:|")
        for r in problem_reports:
            lines.append(
                f"| {r.symbol} | {r.timeframe} | {r.n_bars} | "
                f"{r.n_duplicates} | {r.n_nulls} | {r.is_sorted} | {r.n_gaps} |"
            )
        lines.append("")

        for r in problem_reports:
            issues = r.issues_summary()
            if not issues:
                continue
            lines.append(f"### {r.symbol} {r.timeframe}")
            for issue in issues:
                lines.append(f"- {issue}")
            if r.gap_intervals:
                lines.append("")
                lines.append("Gap intervals (前 10 筆):")
                for prev_t, curr_t in r.gap_intervals[:10]:
                    delta_sec = (curr_t - prev_t).total_seconds()
                    n_missing = int(delta_sec / r.expected_interval_sec) - 1
                    lines.append(f"- `{prev_t}` → `{curr_t}`  (missing ~{n_missing} bars)")
            lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


# -----------------------------------------------------------------------------
# OHLCV parser
# -----------------------------------------------------------------------------
_OHLCV_COLS = ["open", "high", "low", "close", "volume"]


def _ohlcv_rows_to_df(rows: list[list]) -> pd.DataFrame:
    """`[[ts, o, h, l, c, v], ...]` → DF index=DatetimeIndex(UTC)。"""
    if not rows:
        return pd.DataFrame(columns=_OHLCV_COLS).astype(float)

    df = pd.DataFrame(rows, columns=["timestamp", *_OHLCV_COLS])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("timestamp").astype(float)
    df = df[~df.index.duplicated(keep="last")]
    df = df.sort_index()
    return df


# -----------------------------------------------------------------------------
# Public client
# -----------------------------------------------------------------------------
def make_public_client() -> "ccxt.binanceusdm":
    """產生一個 public-only 的幣安永續客戶端，不需要 API key。"""
    client = ccxt.binanceusdm({
        "enableRateLimit": True,
        "options": {"defaultType": "future"},
    })
    return client


# -----------------------------------------------------------------------------
# R3DataLoader
# -----------------------------------------------------------------------------
class R3DataLoader:
    """
    R3 OHLCV loader。

    範例
    ----
    >>> from strategies.r3.config_loader import R3Config
    >>> from strategies.r3.data_loader import R3DataLoader
    >>> loader = R3DataLoader(R3Config.load())
    >>> df = loader.load_ohlcv(
    ...     "BTC/USDT:USDT", "1h",
    ...     start=datetime(2024, 1, 1, tzinfo=timezone.utc),
    ...     end=datetime(2024, 1, 7, tzinfo=timezone.utc),
    ... )
    """

    def __init__(
        self,
        config: R3Config,
        cache_dir: Path | None = None,
        client: "ccxt.Exchange | None" = None,
    ):
        self.config = config
        self.cache_dir = Path(cache_dir) if cache_dir else _default_cache_dir()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._client = client                           # lazy init via property
        self._integrity_log: list[IntegrityReport] = []
        self._api_limits: list[str] = []

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------
    @property
    def client(self) -> "ccxt.Exchange":
        if self._client is None:
            self._client = make_public_client()
        return self._client

    @property
    def integrity_log(self) -> list[IntegrityReport]:
        return list(self._integrity_log)

    @property
    def api_limits(self) -> list[str]:
        return list(self._api_limits)

    def cache_path(self, symbol: str, timeframe: str) -> Path:
        sym_dir = self.cache_dir / _symbol_to_filename(symbol)
        sym_dir.mkdir(parents=True, exist_ok=True)
        return sym_dir / f"{timeframe}.csv"

    def load_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        start: datetime | None = None,
        end: datetime | None = None,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        """
        讀取指定 symbol/timeframe 的 OHLCV。

        - `start` 預設為 90 天前；`end` 預設為 now（皆 UTC）
        - 已有 cache 且覆蓋區間 → 直接讀
        - cache 不足 → 從 API 補 missing 區間，merge 後寫回 cache
        - 完整性檢查在最終 DF 上執行；異常會記入 `integrity_log`
        - **不會 forward fill；也不會插補缺漏 K**
        """
        symbol = symbol.strip()
        if timeframe not in TIMEFRAME_TO_SECONDS:
            raise ValueError(f"Unsupported timeframe: {timeframe}")

        end = end or datetime.now(timezone.utc)
        start = start or (end - timedelta(days=90))

        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("start / end must be timezone-aware (UTC)")

        cache_df = pd.DataFrame() if force_refresh else self._read_cache(symbol, timeframe)

        # 找出 cache 還沒覆蓋的區間，逐一抓取
        missing_ranges = self._compute_missing_ranges(cache_df, start, end, timeframe)
        fetched_frames: list[pd.DataFrame] = [cache_df] if not cache_df.empty else []

        for r_start, r_end in missing_ranges:
            df_part = self._fetch_paginated(symbol, timeframe, r_start, r_end)
            if not df_part.empty:
                fetched_frames.append(df_part)

        if fetched_frames:
            merged = pd.concat(fetched_frames).sort_index()
            merged = merged[~merged.index.duplicated(keep="last")]
        else:
            merged = pd.DataFrame(columns=_OHLCV_COLS).astype(float)

        # 持久化 cache（裸資料，不裁切；裁切只發生在回傳）
        if not merged.empty:
            self._write_cache(symbol, timeframe, merged)

        # 裁切回傳區間
        result = merged.loc[(merged.index >= start) & (merged.index <= end)] if not merged.empty else merged

        # 完整性檢查（針對回傳區間）
        report = check_integrity(result, symbol, timeframe)
        self._integrity_log.append(report)

        return result

    def write_missing_data_report(self) -> Path | None:
        """把累積的 integrity_log + api_limits 落地。"""
        return write_missing_data_report(
            self._integrity_log, self._api_limits, self.cache_dir
        )

    # -------------------------------------------------------------------------
    # Internal — fetch / cache
    # -------------------------------------------------------------------------
    def _fetch_paginated(
        self, symbol: str, timeframe: str,
        start: datetime, end: datetime,
    ) -> pd.DataFrame:
        """從 API 抓 [start, end] 的 OHLCV，自動 pagination。"""
        if start >= end:
            return pd.DataFrame(columns=_OHLCV_COLS).astype(float)

        tf_sec = TIMEFRAME_TO_SECONDS[timeframe]
        since_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)

        all_rows: list[list] = []
        cursor = since_ms

        while cursor < end_ms:
            try:
                rows = self.client.fetch_ohlcv(
                    symbol, timeframe,
                    since=cursor,
                    limit=BINANCE_OHLCV_MAX_PER_CALL,
                )
            except ccxt.BaseError as e:
                self._api_limits.append(
                    f"`{symbol}` `{timeframe}` fetch_ohlcv failed at "
                    f"since={cursor}: {type(e).__name__}: {e}"
                )
                break

            if not rows:
                break

            # 防呆：ccxt 偶爾回傳越界資料
            rows = [r for r in rows if r[0] <= end_ms]
            if not rows:
                break

            all_rows.extend(rows)
            last_open_ms = int(rows[-1][0])

            # 下一頁從最後一根 K 的下一個 bar 開始
            next_cursor = last_open_ms + tf_sec * 1000

            # 已涵蓋到 end_ms → 完成
            if next_cursor >= end_ms:
                break

            if next_cursor <= cursor:
                # 防無限迴圈：API 回傳沒前進
                self._api_limits.append(
                    f"`{symbol}` `{timeframe}` pagination stalled at {cursor}"
                )
                break
            cursor = next_cursor

            # NOTE: 不能用 `len(rows) < limit` 判定結束 —
            # ccxt 對某些 timeframe 內部 cap limit 比我們傳的小（觀察到 5m=1000）。
            # 終止條件純由 cursor 是否到達 end_ms / API 回空 / cursor 卡住決定。

            time.sleep(RATE_LIMIT_SLEEP_SEC)

        return _ohlcv_rows_to_df(all_rows)

    def _read_cache(self, symbol: str, timeframe: str) -> pd.DataFrame:
        path = self.cache_path(symbol, timeframe)
        if not path.exists():
            return pd.DataFrame(columns=_OHLCV_COLS).astype(float)
        try:
            df = pd.read_csv(path, index_col=0)
            # 強制把 index 轉成 UTC-aware DatetimeIndex
            # （pandas parse_dates=True 對含 tz offset 的 string 解析不穩）
            df.index = pd.to_datetime(df.index, utc=True, format="ISO8601")
            df = df[_OHLCV_COLS].astype(float)
            df = df[~df.index.duplicated(keep="last")].sort_index()
            return df
        except Exception as e:
            self._api_limits.append(
                f"cache read failed for `{symbol}` `{timeframe}`: {e}"
            )
            return pd.DataFrame(columns=_OHLCV_COLS).astype(float)

    def _write_cache(self, symbol: str, timeframe: str, df: pd.DataFrame) -> None:
        path = self.cache_path(symbol, timeframe)
        df_to_write = df[_OHLCV_COLS].copy()
        df_to_write.index.name = "timestamp"
        df_to_write.to_csv(path)

    @staticmethod
    def _compute_missing_ranges(
        cache_df: pd.DataFrame,
        start: datetime,
        end: datetime,
        timeframe: str,
    ) -> list[tuple[datetime, datetime]]:
        """
        計算 cache 沒覆蓋的區間。簡化策略：
        - cache 為空 → [(start, end)]
        - cache 有資料 → 補 cache.start 之前 + cache.end 之後（不挖中間洞）
          Reason: gap 由 integrity check 抓出來寫進 missing_data_report.md，
                  不主動「填洞」以避免 silent forward-fill。
        """
        if cache_df.empty:
            return [(start, end)]

        cache_start = cache_df.index.min().to_pydatetime()
        cache_end = cache_df.index.max().to_pydatetime()
        tf_sec = TIMEFRAME_TO_SECONDS[timeframe]

        missing: list[tuple[datetime, datetime]] = []
        if start < cache_start:
            missing.append((start, cache_start - timedelta(seconds=tf_sec)))
        if end > cache_end:
            missing.append((cache_end + timedelta(seconds=tf_sec), end))
        return missing

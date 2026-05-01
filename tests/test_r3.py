"""
R3 Strategy — Unit Tests
========================

Spec   : docs/R3_spec.md
Config : config/r3_strategy.yaml

範圍
----
- Q21~Q29 (Sprint 3+ 策略邏輯) — 多數 SKIPPED 等實作
- Sprint 1 (data + indicators) — 全部實作完整 tests

工程紀律
--------
- 每個 Q 必須至少一個 test，覆蓋 default + 至少一個 edge case
- 測試 fail 時，禁止偷偷調 spec 讓它通過（必須改 code 或回報失敗）
- 所有參數從 R3Config 取，禁止 hardcode
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from strategies.r3.config_loader import R3Config
from strategies.r3 import indicators as ind
from strategies.r3.data_loader import (
    R3DataLoader,
    IntegrityReport,
    check_integrity,
    write_missing_data_report,
    _ohlcv_rows_to_df,
    _symbol_to_filename,
    TIMEFRAME_TO_SECONDS,
)
from strategies.r3.exchange import R3ExchangeData


# ---------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------
@pytest.fixture(scope="module")
def cfg() -> R3Config:
    return R3Config.load()


# ===============================================================
# Sanity check — config 是否完整載入
# ===============================================================
def test_config_loads_without_error(cfg):
    assert cfg.version == "1.0"
    assert cfg.spec_ref == "docs/R3_spec.md"


# ===============================================================
# Q21 — 1H 回踩 EMA20/50 「附近」定義
# ===============================================================
class TestQ21EMAPullbackZone:
    """
    做多：1H low ≤ EMA + 0.3 * ATR_1H
          且 close ∈ [EMA - 0.3*ATR, EMA + 0.3*ATR]
    """

    def test_config_values_match_spec(self, cfg):
        assert cfg.trend_pullback.entry.ema_pullback_atr_mult == 0.3
        assert cfg.trend_pullback.entry.ema_band_atr_mult == 0.3

    def test_long_pullback_touches_ema_within_band_should_qualify(self, cfg):
        pytest.skip("TODO[Sprint-3]: implement after indicators.ema_pullback_zone")

    def test_long_pullback_low_too_far_from_ema_should_not_qualify(self, cfg):
        pytest.skip("TODO[Sprint-3]: low > EMA + 0.3 ATR should fail")

    def test_long_close_outside_band_should_not_qualify(self, cfg):
        pytest.skip("TODO[Sprint-3]: close beyond EMA ± 0.3 ATR should fail")

    def test_short_symmetric(self, cfg):
        pytest.skip("TODO[Sprint-3]: short side mirror logic")


# ===============================================================
# Q22 — RSI 從 40~50 區間重新上彎
# ===============================================================
class TestQ22RSIUptickFromZone:
    """
    做多：過去 5 根 1H 內 min(RSI) ≤ 50
          且 RSI[i] > RSI[i-1]
          且 RSI[i] > 50
    """

    def test_config_values_match_spec(self, cfg):
        assert cfg.trend_pullback.entry.rsi_lookback_bars == 5
        assert cfg.trend_pullback.entry.rsi_threshold == 50

    def test_rsi_was_below_50_now_above_should_qualify(self, cfg):
        pytest.skip("TODO[Sprint-3]: RSI history [42, 47, 49, 51, 53] should pass")

    def test_rsi_never_below_50_should_not_qualify(self, cfg):
        pytest.skip("TODO[Sprint-3]: RSI history [55, 56, 57, 58, 59] should fail")

    def test_rsi_below_50_but_not_uptick_should_not_qualify(self, cfg):
        pytest.skip("TODO[Sprint-3]: RSI [42, 47, 51, 50, 49] should fail (no uptick)")

    def test_short_symmetric(self, cfg):
        pytest.skip("TODO[Sprint-3]: short side: max ≥ 50 + downtick + < 50")


# ===============================================================
# Q23 — 5M 訊號有效窗口（1H 收盤後 12 根 5M 內）
# ===============================================================
class TestQ23SignalValidityWindow:
    def test_config_value_match_spec(self, cfg):
        assert cfg.trend_pullback.signal_validity_window_5m_bars == 12

    def test_signal_at_5m_bar_1_to_12_is_valid(self, cfg):
        pytest.skip("TODO[Sprint-3]: window 1..12 valid")

    def test_signal_at_5m_bar_13_should_be_invalid(self, cfg):
        pytest.skip("TODO[Sprint-3]: bar 13 fails, must wait next 1H")


# ===============================================================
# Q24 — 同幣禁止反向新倉
# ===============================================================
class TestQ24OppositePositionForbidden:
    def test_config_values_match_spec(self, cfg):
        opp = cfg.risk.opposite_position_per_symbol
        assert opp.allow_open_opposite is False
        assert opp.use_hedge_mode is False
        assert opp.force_close_existing is False
        assert opp.wait_until_existing_closed is True

    def test_btc_long_held_short_signal_should_be_rejected(self, cfg):
        pytest.skip("TODO[Sprint-3]: with BTC long open, BTC short signal must be rejected")

    def test_after_existing_closed_new_direction_allowed(self, cfg):
        pytest.skip("TODO[Sprint-3]: after TP/SL exit, opposite direction signal allowed")


# ===============================================================
# Q25 — 均值回歸 5M 止跌/止漲（獨立於 trend）
# ===============================================================
class TestQ25MRConfirmation:
    """
    多單止跌（任二）：
    1. close > open & (close-low)/(high-low) >= 0.6
    2. bullish engulfing or hammer
    3. RSI(14) < 30 & RSI[i] > RSI[i-1]
    """

    def test_config_values_match_spec(self, cfg):
        c = cfg.mean_reversion.confirmation_5m
        assert c.rule == "two_of_three"
        assert c.close_position_in_range_min == 0.6
        assert c.rsi_oversold == 30
        assert c.rsi_overbought == 70

    def test_does_not_use_breakout_signal(self, cfg):
        """MR 不該用『突破前 3 根 high』這種趨勢條件"""
        long_signals = list(cfg.mean_reversion.confirmation_5m.long_signals)
        assert all("breakout" not in s.lower() and "breaks_previous" not in s.lower()
                   for s in long_signals), \
            "Mean reversion confirmation must not use breakout-type signals"

    def test_strong_close_plus_rsi_uptick_should_qualify(self, cfg):
        pytest.skip("TODO[Sprint-4]: 2/3 conditions met")

    def test_only_one_condition_should_not_qualify(self, cfg):
        pytest.skip("TODO[Sprint-4]: only 1/3 fails")


# ===============================================================
# Q26 — Position quantity 用 limit_price 計算
# ===============================================================
class TestQ26QuantityFromLimitPrice:
    def test_config_value_match_spec(self, cfg):
        assert cfg.trend_pullback.entry_order.quantity_basis == "limit_price"

    def test_quantity_formula_uses_limit_price_not_current(self, cfg):
        pytest.skip("TODO[Sprint-3]: risk_amount / |limit-stop| = qty (NOT using current bid)")


# ===============================================================
# Q27 — 部分成交處理
# ===============================================================
class TestQ27PartialFill:
    def test_config_values_match_spec(self, cfg):
        pf = cfg.trend_pullback.entry_order.partial_fill
        assert pf.treat_filled_as_entry is True
        assert pf.cancel_remaining_after_timeout is True

    def test_filled_portion_immediately_protected_by_sl(self, cfg):
        pytest.skip("TODO[Sprint-3]: partial fill -> immediate reduce-only SL")

    def test_unfilled_portion_canceled_at_timeout(self, cfg):
        pytest.skip("TODO[Sprint-3]: 2-bar timeout -> cancel remaining qty")

    def test_r_multiple_uses_actual_filled_qty(self, cfg):
        pytest.skip("TODO[Sprint-3]: R = filled_qty * sl_distance, not requested_qty")


# ===============================================================
# Q28 — Equity 基準（保守算法）
# ===============================================================
class TestQ28EquityBasis:
    def test_config_formula_match_spec(self, cfg):
        formula = cfg.risk.equity_basis.formula
        assert "wallet_balance" in formula
        assert "realized_pnl" in formula
        assert "unrealized_pnl" in formula

    def test_positive_unrealized_does_not_inflate_equity(self, cfg):
        pytest.skip("TODO[Sprint-3]: wallet=5000, +unrealized=200 -> equity=5000 (not 5200)")

    def test_negative_unrealized_immediately_reduces_equity(self, cfg):
        pytest.skip("TODO[Sprint-3]: wallet=5000, -unrealized=300 -> equity=4700")


# ===============================================================
# Q29 — 同策略連續訊號 Cooldown
# ===============================================================
class TestQ29SameStrategyCooldown:
    def test_config_values_match_spec(self, cfg):
        cd = cfg.trend_pullback.cooldown_after
        assert cd.sl_exit_1h_bars == 1
        assert cd.tp_exit_1h_bars == 0

    def test_sl_exit_blocks_next_1h_signal(self, cfg):
        pytest.skip("TODO[Sprint-3]: BTC trend SL at T -> reject BTC trend signal at T+1H")

    def test_tp_exit_does_not_block(self, cfg):
        pytest.skip("TODO[Sprint-3]: BTC trend TP at T -> allow BTC trend signal at T+1H")

    def test_cooldown_only_applies_per_symbol_per_strategy(self, cfg):
        pytest.skip("TODO[Sprint-3]: BTC SL does not block ETH trend, nor BTC mean_reversion")


# ===============================================================
# ===============================================================
# Sprint 1 — Data Layer
# ===============================================================
# ===============================================================

UTC = timezone.utc


def _make_clean_ohlcv(
    n: int,
    timeframe: str = "1h",
    start: datetime = datetime(2026, 1, 1, tzinfo=UTC),
    seed: int = 0,
) -> pd.DataFrame:
    """產生一份乾淨的合成 OHLCV（連續、無 null、有 volume）。"""
    rng = np.random.default_rng(seed)
    sec = TIMEFRAME_TO_SECONDS[timeframe]
    idx = pd.date_range(start, periods=n, freq=f"{sec}s", tz=UTC)
    close = 100.0 + rng.standard_normal(n).cumsum()
    df = pd.DataFrame({
        "open":   np.r_[100.0, close[:-1]],
        "high":   close + np.abs(rng.standard_normal(n)) * 0.3,
        "low":    close - np.abs(rng.standard_normal(n)) * 0.3,
        "close":  close,
        "volume": np.abs(rng.standard_normal(n)) * 100 + 50,
    }, index=idx)
    df.index.name = "timestamp"
    return df


# ---------------------------------------------------------------
# check_integrity
# ---------------------------------------------------------------
class TestIntegrityChecks:
    def test_clean_ohlcv_passes(self):
        df = _make_clean_ohlcv(48, "1h")
        report = check_integrity(df, "BTC/USDT:USDT", "1h")
        assert report.is_clean
        assert report.n_bars == 48
        assert report.n_duplicates == 0
        assert report.n_nulls == 0
        assert report.is_sorted is True
        assert report.n_gaps == 0
        assert report.expected_interval_sec == 3600

    def test_duplicated_timestamp_detected(self):
        df = _make_clean_ohlcv(10, "1h")
        df = pd.concat([df, df.iloc[[3]]]).sort_index()
        report = check_integrity(df, "X", "1h")
        assert report.n_duplicates == 1
        assert not report.is_clean

    def test_null_ohlcv_detected(self):
        df = _make_clean_ohlcv(10, "1h")
        df.iloc[5, df.columns.get_loc("close")] = np.nan
        df.iloc[7, df.columns.get_loc("volume")] = np.nan
        report = check_integrity(df, "X", "1h")
        assert report.n_nulls == 2
        assert not report.is_clean

    def test_unsorted_index_detected(self):
        df = _make_clean_ohlcv(10, "1h")
        # 反轉排序
        df = df.iloc[::-1]
        report = check_integrity(df, "X", "1h")
        assert report.is_sorted is False
        assert not report.is_clean

    def test_time_gap_detected(self):
        df = _make_clean_ohlcv(10, "1h")
        # 移除中間 2 根 → 製造 3h gap
        df = df.drop(df.index[4:6])
        report = check_integrity(df, "X", "1h")
        assert report.n_gaps == 1
        assert not report.is_clean
        assert len(report.gap_intervals) == 1

    def test_5m_interval_seconds(self):
        df = _make_clean_ohlcv(20, "5m")
        report = check_integrity(df, "X", "5m")
        assert report.expected_interval_sec == 300
        assert report.is_clean

    def test_4h_interval_seconds(self):
        df = _make_clean_ohlcv(20, "4h")
        report = check_integrity(df, "X", "4h")
        assert report.expected_interval_sec == 14400
        assert report.is_clean

    def test_empty_dataframe_returns_clean_report(self):
        df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"]).astype(float)
        df.index = pd.DatetimeIndex([], tz=UTC, name="timestamp")
        report = check_integrity(df, "X", "1h")
        assert report.n_bars == 0
        assert report.is_clean


# ---------------------------------------------------------------
# Symbol filename mapping
# ---------------------------------------------------------------
class TestSymbolToFilename:
    def test_ccxt_unified_to_native(self):
        assert _symbol_to_filename("BTC/USDT:USDT") == "BTCUSDT"

    def test_already_native(self):
        assert _symbol_to_filename("BTCUSDT") == "BTCUSDT"

    def test_lowercase_normalized(self):
        assert _symbol_to_filename("btc/usdt:usdt") == "BTCUSDT"


# ---------------------------------------------------------------
# OHLCV row parser
# ---------------------------------------------------------------
class TestOhlcvParser:
    def test_parse_empty(self):
        df = _ohlcv_rows_to_df([])
        assert df.empty
        assert list(df.columns) == ["open", "high", "low", "close", "volume"]

    def test_parse_dedup_and_sort(self):
        rows = [
            [1735689600000, 1, 2, 0.5, 1.5, 100],   # 2025-01-01 00:00
            [1735693200000, 2, 3, 1.5, 2.5, 200],   # 2025-01-01 01:00
            [1735689600000, 9, 9, 9.0, 9.0, 999],   # 重複 timestamp，後到的勝
        ]
        df = _ohlcv_rows_to_df(rows)
        assert len(df) == 2
        # 同 timestamp 的後到項覆蓋
        assert df.iloc[0]["close"] == 9.0
        assert df.index.is_monotonic_increasing

    def test_index_is_utc(self):
        rows = [[1735689600000, 1, 2, 0.5, 1.5, 100]]
        df = _ohlcv_rows_to_df(rows)
        assert df.index.tz is not None
        assert str(df.index.tz) in ("UTC", "tzutc()")


# ---------------------------------------------------------------
# Compute missing ranges
# ---------------------------------------------------------------
class TestMissingRangeCalculation:
    def setup_method(self):
        self.start = datetime(2026, 1, 1, tzinfo=UTC)
        self.end = datetime(2026, 1, 10, tzinfo=UTC)

    def test_empty_cache_returns_full_range(self):
        empty = pd.DataFrame()
        ranges = R3DataLoader._compute_missing_ranges(empty, self.start, self.end, "1h")
        assert ranges == [(self.start, self.end)]

    def test_cache_covers_fully_returns_empty(self):
        cache = _make_clean_ohlcv(24 * 12, "1h", start=self.start - timedelta(days=1))
        ranges = R3DataLoader._compute_missing_ranges(cache, self.start, self.end, "1h")
        # Cache 覆蓋 -1 ~ +11，請求 0 ~ 9，cache_end >= end → 沒有右側缺口
        # cache_start (-1) <= start (0) → 沒有左側缺口
        assert ranges == []

    def test_cache_covers_head_only_fetches_tail(self):
        # cache: 1/1 ~ 1/3
        cache = _make_clean_ohlcv(48, "1h", start=self.start)
        ranges = R3DataLoader._compute_missing_ranges(cache, self.start, self.end, "1h")
        assert len(ranges) == 1
        assert ranges[0][0] > cache.index.max().to_pydatetime()
        assert ranges[0][1] == self.end

    def test_cache_covers_tail_only_fetches_head(self):
        # cache: 1/5 ~ 1/15
        cache = _make_clean_ohlcv(24 * 10, "1h", start=datetime(2026, 1, 5, tzinfo=UTC))
        ranges = R3DataLoader._compute_missing_ranges(cache, self.start, self.end, "1h")
        assert len(ranges) == 1
        assert ranges[0][0] == self.start
        assert ranges[0][1] < cache.index.min().to_pydatetime()


# ---------------------------------------------------------------
# Cache write / read roundtrip
# ---------------------------------------------------------------
class TestDataLoaderCache:
    def test_cache_roundtrip(self, cfg, tmp_path):
        loader = R3DataLoader(cfg, cache_dir=tmp_path)
        df = _make_clean_ohlcv(24, "1h")
        loader._write_cache("BTC/USDT:USDT", "1h", df)

        path = loader.cache_path("BTC/USDT:USDT", "1h")
        assert path.exists()
        assert path.parent.name == "BTCUSDT"
        assert path.name == "1h.csv"

        loaded = loader._read_cache("BTC/USDT:USDT", "1h")
        assert len(loaded) == len(df)
        assert loaded.index.tz is not None
        # 數值比對（csv round-trip 後 index.freq 會遺失，故 check_freq=False）
        pd.testing.assert_frame_equal(
            loaded.sort_index(),
            df[loaded.columns].sort_index(),
            check_exact=False,
            atol=1e-9,
            check_freq=False,
        )

    def test_cache_miss_returns_empty_df(self, cfg, tmp_path):
        loader = R3DataLoader(cfg, cache_dir=tmp_path)
        loaded = loader._read_cache("ETH/USDT:USDT", "5m")
        assert loaded.empty
        assert list(loaded.columns) == ["open", "high", "low", "close", "volume"]

    def test_cache_read_handles_tz_aware_csv_strings(self, cfg, tmp_path):
        """
        Regression for smoke-test bug: pandas `parse_dates=True` 對含 +00:00 offset
        的 timestamp 字串解析不穩，會回傳 str Index，導致 `.tz` AttributeError。
        必須用 `pd.to_datetime(..., utc=True, format='ISO8601')` 強制解析。
        """
        sym_dir = tmp_path / "BTCUSDT"
        sym_dir.mkdir(parents=True)
        path = sym_dir / "1h.csv"
        # 模擬實際 cache 寫出格式（含 +00:00）
        path.write_text(
            "timestamp,open,high,low,close,volume\n"
            "2026-04-24 08:00:00+00:00,100.0,101.0,99.0,100.5,500.0\n"
            "2026-04-24 09:00:00+00:00,100.5,102.0,100.0,101.5,600.0\n",
            encoding="utf-8",
        )
        loader = R3DataLoader(cfg, cache_dir=tmp_path)
        loaded = loader._read_cache("BTC/USDT:USDT", "1h")
        assert len(loaded) == 2
        assert isinstance(loaded.index, pd.DatetimeIndex)
        assert loaded.index.tz is not None
        # 確認沒有觸發 fallback path（API limits 不應為這個原因增加）
        assert all("'Index' object has no attribute" not in note
                   for note in loader.api_limits)

    def test_cache_read_handles_mixed_microsecond_precision(self, cfg, tmp_path):
        """
        Regression #2: 真實 funding rate cache 內，第一行可能精度只到秒
        (08:00:00+00:00) 但後續行帶微秒 (08:00:00.002000+00:00)。
        舊的 `pd.to_datetime(...)` 會根據首行推斷嚴格格式 → 後續行解析失敗。
        必須用 format='ISO8601' 容忍混合精度。
        """
        sym_dir = tmp_path / "BTCUSDT"
        sym_dir.mkdir(parents=True)
        path = sym_dir / "1h.csv"
        path.write_text(
            "timestamp,open,high,low,close,volume\n"
            "2026-04-24 08:00:00+00:00,100.0,101.0,99.0,100.5,500.0\n"
            "2026-04-25 08:00:00.002000+00:00,100.5,102.0,100.0,101.5,600.0\n"
            "2026-04-26 16:00:00.500+00:00,101.5,103.0,101.0,102.5,700.0\n",
            encoding="utf-8",
        )
        loader = R3DataLoader(cfg, cache_dir=tmp_path)
        loaded = loader._read_cache("BTC/USDT:USDT", "1h")
        assert len(loaded) == 3
        assert isinstance(loaded.index, pd.DatetimeIndex)
        assert loader.api_limits == []


# ---------------------------------------------------------------
# write_missing_data_report
# ---------------------------------------------------------------
class TestMissingDataReport:
    def test_no_issues_no_file(self, tmp_path):
        clean = IntegrityReport(
            symbol="BTCUSDT", timeframe="1h", n_bars=100,
            n_duplicates=0, n_nulls=0, is_sorted=True,
            expected_interval_sec=3600, n_gaps=0,
        )
        result = write_missing_data_report([clean], [], tmp_path)
        assert result is None
        assert not (tmp_path / "missing_data_report.md").exists()

    def test_problem_report_writes_file(self, tmp_path):
        bad = IntegrityReport(
            symbol="BTCUSDT", timeframe="5m", n_bars=100,
            n_duplicates=2, n_nulls=1, is_sorted=True,
            expected_interval_sec=300, n_gaps=3,
            gap_intervals=[
                (pd.Timestamp("2026-01-01 00:00", tz=UTC),
                 pd.Timestamp("2026-01-01 00:30", tz=UTC)),
            ],
        )
        result = write_missing_data_report([bad], [], tmp_path)
        assert result is not None
        assert result.exists()
        text = result.read_text(encoding="utf-8")
        assert "BTCUSDT" in text
        assert "5m" in text
        assert "duplicated" in text
        assert "null" in text
        assert "gaps" in text or "gap" in text

    def test_api_limit_writes_file_even_with_clean_reports(self, tmp_path):
        clean = IntegrityReport(
            symbol="BTCUSDT", timeframe="1h", n_bars=100,
            n_duplicates=0, n_nulls=0, is_sorted=True,
            expected_interval_sec=3600, n_gaps=0,
        )
        api_limit = ["BTCUSDT 1h fetch_ohlcv failed: timeout"]
        result = write_missing_data_report([clean], api_limit, tmp_path)
        assert result is not None
        text = result.read_text(encoding="utf-8")
        assert "API" in text
        assert "timeout" in text

    def test_stale_report_removed_when_issues_resolved(self, tmp_path):
        report_path = tmp_path / "missing_data_report.md"
        report_path.write_text("# old report", encoding="utf-8")

        clean = IntegrityReport(
            symbol="BTCUSDT", timeframe="1h", n_bars=100,
            n_duplicates=0, n_nulls=0, is_sorted=True,
            expected_interval_sec=3600, n_gaps=0,
        )
        result = write_missing_data_report([clean], [], tmp_path)
        assert result is None
        assert not report_path.exists()


# ---------------------------------------------------------------
# Pagination logic — fake client
# ---------------------------------------------------------------
class _FakeClient:
    """假 ccxt client，用於驗證 pagination 不打真 API。"""
    def __init__(self, all_rows: list[list], page_size: int = 5):
        self.all_rows = all_rows
        self.page_size = page_size
        self.calls: list[dict] = []

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        self.calls.append({"since": since, "limit": limit})
        # 取 since 之後的前 page_size 筆
        rows = [r for r in self.all_rows if r[0] >= since]
        return rows[:self.page_size]


class TestDataLoaderPagination:
    def test_paginated_fetch_loops_until_end_ms(self, cfg, tmp_path):
        """
        關鍵：終止由 cursor 是否到達 end_ms 主導，**不**依賴 len(rows)<limit。
        這個 bug 在 smoke test 抓到（5m ccxt 內部把 limit cap 在 1000）。
        """
        import strategies.r3.data_loader as dl

        class CappedPageClient:
            """模擬 ccxt 內部 cap：無論你傳 limit 多少，只回最多 page_cap 筆。"""
            def __init__(self, rows, page_cap):
                self.rows = rows
                self.page_cap = page_cap
                self.calls: list[int] = []

            def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
                self.calls.append(since)
                page = [r for r in self.rows if r[0] >= since][: self.page_cap]
                return page

        sec = TIMEFRAME_TO_SECONDS["1h"]
        start_dt = datetime(2026, 1, 1, tzinfo=UTC)
        end_dt = datetime(2026, 1, 2, tzinfo=UTC)
        start_ms = int(start_dt.timestamp() * 1000)
        rows = [
            [start_ms + i * sec * 1000, 1, 2, 0.5, 1.5, 100]
            for i in range(24)
        ]
        # ccxt 內部 cap = 8，但 R3 期望 1500 — 應觸發 3 頁
        client = CappedPageClient(rows, page_cap=8)

        original_sleep = dl.RATE_LIMIT_SLEEP_SEC
        dl.RATE_LIMIT_SLEEP_SEC = 0.0
        try:
            loader = R3DataLoader(cfg, cache_dir=tmp_path, client=client)
            df = loader._fetch_paginated(
                "BTC/USDT:USDT", "1h", start_dt, end_dt,
            )
        finally:
            dl.RATE_LIMIT_SLEEP_SEC = original_sleep

        # 24 根全抓到（即使 ccxt cap 在 8/page）
        assert len(df) == 24
        # 至少 3 頁
        assert len(client.calls) >= 3

    def test_paginated_stops_at_end_ms(self, cfg, tmp_path):
        """請求結束時間後不再繼續抓（即使 API 還有資料）。"""
        import strategies.r3.data_loader as dl

        class GreedyClient:
            """無限資料：永遠回傳 since 之後的 limit 筆。"""
            def __init__(self):
                self.calls = 0

            def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
                self.calls += 1
                sec = TIMEFRAME_TO_SECONDS[timeframe]
                # 不限數量，從 since 一路往未來生 limit 根
                rows = [
                    [since + i * sec * 1000, 1, 2, 0.5, 1.5, 100]
                    for i in range(limit)
                ]
                return rows

        client = GreedyClient()
        original_sleep = dl.RATE_LIMIT_SLEEP_SEC
        dl.RATE_LIMIT_SLEEP_SEC = 0.0
        try:
            loader = R3DataLoader(cfg, cache_dir=tmp_path, client=client)
            df = loader._fetch_paginated(
                "BTC/USDT:USDT", "1h",
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2026, 1, 1, 5, tzinfo=UTC),  # 只要 5 小時
            )
        finally:
            dl.RATE_LIMIT_SLEEP_SEC = original_sleep

        # 只接受 <= end_ms 的 bars
        assert len(df) <= 6  # 0,1,2,3,4,5h
        assert df.index.max() <= datetime(2026, 1, 1, 5, tzinfo=UTC)


# ---------------------------------------------------------------
# Exchange data — config wiring (no live API)
# ---------------------------------------------------------------
class TestR3ExchangeDataConfig:
    def test_instantiates_with_config(self, cfg, tmp_path):
        ex = R3ExchangeData(cfg, cache_dir=tmp_path)
        assert ex.config is cfg
        assert ex.cache_dir == tmp_path
        assert ex.api_limits == []

    def test_funding_lookback_from_config(self, cfg, tmp_path):
        ex = R3ExchangeData(cfg, cache_dir=tmp_path)
        assert cfg.funding.lookback_days == 90
        assert cfg.funding.min_samples_required == 120

    def test_validates_utc_aware_datetimes(self, cfg, tmp_path):
        ex = R3ExchangeData(cfg, cache_dir=tmp_path)
        with pytest.raises(ValueError, match="timezone-aware"):
            ex._validate_utc(
                datetime(2026, 1, 1),  # naive
                datetime(2026, 1, 2, tzinfo=UTC),
            )


# ===============================================================
# ===============================================================
# Sprint 1 — Indicators
# ===============================================================
# ===============================================================

# ---------------------------------------------------------------
# EMA
# ---------------------------------------------------------------
class TestEMA:
    def test_constant_input_constant_output(self):
        s = pd.Series([5.0] * 50)
        out = ind.ema(s, period=10)
        # EMA of constant = constant
        assert (out == 5.0).all()

    def test_ema_period_must_be_positive(self):
        with pytest.raises(ValueError):
            ind.ema(pd.Series([1.0, 2.0]), period=0)

    def test_first_value_equals_first_input_with_adjust_false(self):
        s = pd.Series([10.0, 20.0, 30.0])
        out = ind.ema(s, period=5)
        # adjust=False 下，第一個值 = 第一個輸入
        assert out.iloc[0] == 10.0


# ---------------------------------------------------------------
# RSI
# ---------------------------------------------------------------
class TestRSI:
    def test_period_must_be_positive(self):
        with pytest.raises(ValueError):
            ind.rsi(pd.Series([1.0]), period=0)

    def test_pure_uptrend_rsi_approaches_100(self):
        s = pd.Series(np.arange(1, 100, dtype=float))
        out = ind.rsi(s, period=14)
        # 純上升序列：avg_loss = 0 → RSI = 100
        last = out.dropna().iloc[-1]
        assert last == 100.0

    def test_first_period_values_are_nan(self):
        s = pd.Series(np.arange(1, 30, dtype=float))
        out = ind.rsi(s, period=14)
        # 前 14 根（min_periods=14）為 NaN
        assert out.iloc[:14].isna().all()


# ---------------------------------------------------------------
# ADX
# ---------------------------------------------------------------
class TestADX:
    def test_adx_in_0_to_100_range(self):
        df = _make_clean_ohlcv(200, "1h", seed=42)
        out = ind.adx(df["high"], df["low"], df["close"], 14)
        valid = out.dropna()
        assert (valid >= 0).all()
        assert (valid <= 100).all()

    def test_strong_uptrend_has_higher_adx_than_choppy(self):
        n = 200
        idx = pd.date_range("2026-01-01", periods=n, freq="1h", tz=UTC)
        # 強趨勢
        trend_close = pd.Series(np.linspace(100, 200, n), index=idx)
        trend_df = pd.DataFrame({
            "high": trend_close + 0.1,
            "low": trend_close - 0.1,
            "close": trend_close,
        }, index=idx)
        # 盤整
        chop_close = pd.Series(100 + np.sin(np.arange(n) * 0.5) * 1.0, index=idx)
        chop_df = pd.DataFrame({
            "high": chop_close + 0.5,
            "low": chop_close - 0.5,
            "close": chop_close,
        }, index=idx)

        adx_trend = ind.adx(trend_df["high"], trend_df["low"], trend_df["close"], 14).dropna().iloc[-1]
        adx_chop = ind.adx(chop_df["high"], chop_df["low"], chop_df["close"], 14).dropna().iloc[-1]
        assert adx_trend > adx_chop


# ---------------------------------------------------------------
# ATR / ATR_pct
# ---------------------------------------------------------------
class TestATR:
    def test_atr_non_negative(self):
        df = _make_clean_ohlcv(100, "1h", seed=1)
        out = ind.atr(df["high"], df["low"], df["close"], 14)
        valid = out.dropna()
        assert (valid >= 0).all()

    def test_atr_pct_equals_atr_div_close(self):
        df = _make_clean_ohlcv(100, "1h", seed=1)
        a = ind.atr(df["high"], df["low"], df["close"], 14)
        ap = ind.atr_pct(df["high"], df["low"], df["close"], 14)
        diff = (ap - a / df["close"]).abs().dropna()
        assert (diff < 1e-12).all()


# ---------------------------------------------------------------
# Extreme Vol — Q5 / Q13 三段式
# ---------------------------------------------------------------
class TestExtremeVolQ13:
    def _policy(self, cfg):
        return ind.warmup_policy_from_config(cfg)

    def test_warmup_policy_loaded_from_config(self, cfg):
        policy = self._policy(cfg)
        assert policy.day_30_threshold_atr_pct == 0.04
        assert policy.rolling_lookback_days == 90
        assert policy.rolling_percentile == 95
        assert policy.day_30_trade_allowed is False
        assert policy.day_31_to_90_trade_allowed is True

    def test_day_1_to_30_always_returns_false(self, cfg):
        # 24 bars/day for 1h → 30 days = 720 bars
        # 製造 1000 根，前 720 根的 atr_pct 全部高到爆，仍應為 False
        n = 1000
        atr_pct = pd.Series([0.10] * n, index=pd.RangeIndex(n))
        out = ind.extreme_vol(atr_pct, bars_per_day=24, policy=self._policy(cfg))
        assert (out.iloc[:720] == False).all()

    def test_day_31_to_90_uses_fixed_threshold(self, cfg):
        bpd = 24
        n = 91 * bpd
        # 第 31~90 天：atr_pct = 0.045 (> 0.04 threshold)
        atr_pct = pd.Series([0.045] * n, index=pd.RangeIndex(n))
        out = ind.extreme_vol(atr_pct, bars_per_day=bpd, policy=self._policy(cfg))
        seg2 = out.iloc[30 * bpd:90 * bpd]
        assert seg2.all()  # 全部 True

    def test_day_31_to_90_below_threshold_returns_false(self, cfg):
        bpd = 24
        n = 91 * bpd
        atr_pct = pd.Series([0.03] * n, index=pd.RangeIndex(n))
        out = ind.extreme_vol(atr_pct, bars_per_day=bpd, policy=self._policy(cfg))
        seg2 = out.iloc[30 * bpd:90 * bpd]
        assert not seg2.any()

    def test_day_91_plus_uses_rolling_percentile(self, cfg):
        bpd = 24
        # 製造 92 天的資料：前 90 天 atr_pct=0.02，最後 1 天 atr_pct=0.10（極端）
        n = 92 * bpd
        atr_pct = pd.Series([0.02] * (91 * bpd) + [0.10] * bpd, index=pd.RangeIndex(n))
        out = ind.extreme_vol(atr_pct, bars_per_day=bpd, policy=self._policy(cfg))
        # 最後 24 根（Day 92）應該 True，因為 0.10 > 90D rolling 95% percentile (≈ 0.02)
        assert out.iloc[-bpd:].all()


# ---------------------------------------------------------------
# Bollinger Bands
# ---------------------------------------------------------------
class TestBollingerBands:
    def test_band_ordering(self, cfg):
        bb_cfg = cfg.mean_reversion.bollinger
        df = _make_clean_ohlcv(100, "1h", seed=2)
        bands = ind.bollinger_bands(df["close"], bb_cfg.period, bb_cfg.std_multiplier)
        valid = pd.concat([bands.lower, bands.middle, bands.upper], axis=1).dropna()
        assert (valid.iloc[:, 0] <= valid.iloc[:, 1]).all()
        assert (valid.iloc[:, 1] <= valid.iloc[:, 2]).all()

    def test_constant_close_zero_width(self, cfg):
        bb_cfg = cfg.mean_reversion.bollinger
        s = pd.Series([100.0] * 50)
        bands = ind.bollinger_bands(s, bb_cfg.period, bb_cfg.std_multiplier)
        # 常數序列：std=0 → upper=middle=lower
        valid = bands.upper.dropna()
        assert (valid == 100.0).all()

    def test_first_period_minus_one_values_nan(self, cfg):
        bb_cfg = cfg.mean_reversion.bollinger
        s = pd.Series(np.arange(1, 50, dtype=float))
        bands = ind.bollinger_bands(s, bb_cfg.period, bb_cfg.std_multiplier)
        # period-1 根 NaN
        assert bands.middle.iloc[: bb_cfg.period - 1].isna().all()


# ---------------------------------------------------------------
# VWAP daily reset
# ---------------------------------------------------------------
class TestVWAPDailyReset:
    def test_first_bar_each_day_equals_typical_price(self):
        # 兩天的 1h 資料，volume 全部固定
        days = [
            datetime(2026, 1, 1, h, tzinfo=UTC) for h in range(24)
        ] + [
            datetime(2026, 1, 2, h, tzinfo=UTC) for h in range(24)
        ]
        idx = pd.DatetimeIndex(days)
        high = pd.Series(np.arange(48, dtype=float) + 101, index=idx)
        low = pd.Series(np.arange(48, dtype=float) + 99, index=idx)
        close = pd.Series(np.arange(48, dtype=float) + 100, index=idx)
        volume = pd.Series([100.0] * 48, index=idx)

        vwap = ind.vwap_daily(high, low, close, volume)

        # 每天第一根 K：VWAP 應等於該根的 typical price
        first_d1 = vwap.iloc[0]
        tp_d1 = (high.iloc[0] + low.iloc[0] + close.iloc[0]) / 3
        assert abs(first_d1 - tp_d1) < 1e-9

        first_d2 = vwap.iloc[24]
        tp_d2 = (high.iloc[24] + low.iloc[24] + close.iloc[24]) / 3
        assert abs(first_d2 - tp_d2) < 1e-9

    def test_requires_utc_index(self):
        idx_naive = pd.date_range("2026-01-01", periods=10, freq="1h")
        s = pd.Series([1.0] * 10, index=idx_naive)
        with pytest.raises(ValueError, match="UTC"):
            ind.vwap_daily(s, s, s, s)

    def test_zero_volume_returns_nan(self):
        idx = pd.date_range("2026-01-01", periods=5, freq="1h", tz=UTC)
        s = pd.Series([100.0] * 5, index=idx)
        vol = pd.Series([0.0] * 5, index=idx)
        vwap = ind.vwap_daily(s, s, s, vol)
        assert vwap.isna().all()


# ---------------------------------------------------------------
# VWAP deviation band
# ---------------------------------------------------------------
class TestVWAPDeviation:
    def test_band_ordering(self, cfg):
        df = _make_clean_ohlcv(72, "1h", seed=3)
        vwap = ind.vwap_daily(df["high"], df["low"], df["close"], df["volume"])
        upper, lower, stdev = ind.vwap_deviation_band(
            df["close"], vwap,
            lookback_hours=cfg.mean_reversion.vwap_deviation.lookback_hours,
            multiplier=cfg.mean_reversion.vwap_deviation.multiplier,
            bars_per_hour=1,
        )
        valid = pd.concat([lower, upper], axis=1).dropna()
        assert (valid.iloc[:, 0] <= valid.iloc[:, 1]).all()


# ---------------------------------------------------------------
# funding_z
# ---------------------------------------------------------------
class TestFundingZ:
    def test_insufficient_samples_returns_nan(self, cfg):
        # 只有 50 個 funding events，min_samples=120 → 全 NaN
        rates = pd.Series(np.random.default_rng(0).normal(0.0001, 0.0001, 50))
        z = ind.funding_z(
            rates,
            lookback_days=cfg.funding.lookback_days,
            funding_interval_hours=cfg.funding.default_interval_hours,
            min_samples=cfg.funding.min_samples_required,
        )
        assert z.isna().all()

    def test_sufficient_samples_returns_finite(self, cfg):
        # 270 events 足夠 (lookback=90d * 24/8)
        rng = np.random.default_rng(1)
        rates = pd.Series(rng.normal(0.0001, 0.0002, 400))
        z = ind.funding_z(
            rates,
            lookback_days=cfg.funding.lookback_days,
            funding_interval_hours=cfg.funding.default_interval_hours,
            min_samples=cfg.funding.min_samples_required,
        )
        # 後段應該有有限值
        valid = z.dropna()
        assert len(valid) > 0
        assert np.isfinite(valid).all()

    def test_z_score_centered_around_zero_for_stationary_series(self, cfg):
        rng = np.random.default_rng(7)
        rates = pd.Series(rng.normal(0.0, 0.001, 1000))
        z = ind.funding_z(
            rates,
            lookback_days=cfg.funding.lookback_days,
            funding_interval_hours=cfg.funding.default_interval_hours,
            min_samples=cfg.funding.min_samples_required,
        )
        valid = z.dropna()
        # 應大致對稱於 0
        assert abs(valid.mean()) < 0.5


# ---------------------------------------------------------------
# premium_z
# ---------------------------------------------------------------
class TestPremiumZ:
    def test_insufficient_samples_returns_nan(self):
        s = pd.Series([0.0001] * 30)
        z = ind.premium_z(s, window=100, min_samples=50)
        assert z.isna().all()

    def test_outlier_has_large_z(self):
        rng = np.random.default_rng(2)
        s = pd.Series(np.r_[rng.normal(0.0, 0.0001, 200), [0.01]])
        z = ind.premium_z(s, window=100, min_samples=50)
        # 最後一筆是異常值，z 應該很大
        assert abs(z.iloc[-1]) > 5


# ---------------------------------------------------------------
# Confirmed Pivot — Q9 / Q14
# ---------------------------------------------------------------
class TestConfirmedPivotQ9Q14:
    def test_pivot_value_appears_after_confirm_delay(self):
        # 構造：第 10 根是 pivot high，左右各 5 根都比它低
        n = 30
        highs = pd.Series([1.0] * n)
        highs.iloc[10] = 100.0
        # n=5, confirm_delay=5 → 第 15 根才標記 pivot
        confirmed = ind.pivot_high(highs, n=5, confirm_delay_bars=5)
        assert pd.isna(confirmed.iloc[10])
        assert confirmed.iloc[15] == 100.0

    def test_pivot_low_symmetric(self):
        n = 30
        lows = pd.Series([100.0] * n)
        lows.iloc[10] = 1.0
        confirmed = ind.pivot_low(lows, n=5, confirm_delay_bars=5)
        assert pd.isna(confirmed.iloc[10])
        assert confirmed.iloc[15] == 1.0

    def test_pivot_no_lookahead(self):
        """關鍵測試：pivot 在 i 確認前，不能在 i 之前的任何 bar 出現。"""
        n = 50
        highs = pd.Series([1.0] * n)
        highs.iloc[20] = 100.0  # 唯一 pivot
        confirmed = ind.pivot_high(highs, n=5, confirm_delay_bars=5)
        # 在第 25 根之前（含 pivot 自己）都不能有值
        before = confirmed.iloc[:25]
        assert before.isna().all()
        # 第 25 根才出現
        assert confirmed.iloc[25] == 100.0

    def test_confirm_delay_must_be_at_least_n(self):
        with pytest.raises(ValueError, match="confirm_delay_bars"):
            ind.pivot_high(pd.Series([1.0] * 10), n=5, confirm_delay_bars=3)

    def test_n_must_be_positive(self):
        with pytest.raises(ValueError):
            ind.pivot_high(pd.Series([1.0] * 10), n=0, confirm_delay_bars=5)

    def test_normal_pivot_uses_config_n5(self, cfg):
        """確認 config 的 normal pivot 是 N=5, delay=5"""
        assert cfg.pivot.normal.n == 5
        assert cfg.pivot.normal.confirm_delay_bars == 5

    def test_tight_trailing_pivot_uses_config_n3(self, cfg):
        """確認 config 的 tight trailing pivot 是 N=3, delay=3 (Q14)"""
        assert cfg.pivot.tight_trailing.n == 3
        assert cfg.pivot.tight_trailing.confirm_delay_bars == 3

    def test_latest_confirmed_pivot_returns_last(self):
        n = 40
        highs = pd.Series([1.0] * n)
        highs.iloc[10] = 50.0
        highs.iloc[25] = 80.0
        confirmed = ind.pivot_high(highs, n=5, confirm_delay_bars=5)
        idx, val = ind.latest_confirmed_pivot(confirmed, as_of_index=39)
        assert val == 80.0


# ---------------------------------------------------------------
# Candle Patterns
# ---------------------------------------------------------------
class TestCandlePatterns:
    def test_strong_close_detects_textbook_bullish(self, cfg):
        cf = cfg.trend_pullback.confirmation_5m
        # open=10, high=20, low=9, close=19
        # body=9, range=11
        # close_pos = (19-9)/(20-9) = 10/11 ≈ 0.909 ≥ 0.7
        # body_ratio = 9/11 ≈ 0.818 ≥ 0.5
        df = pd.DataFrame({
            "open": [10.0],
            "high": [20.0],
            "low":  [9.0],
            "close": [19.0],
        })
        out = ind.strong_close(
            df["open"], df["high"], df["low"], df["close"],
            close_position_min=cf.strong_close.close_position_min,
            body_ratio_min=cf.strong_close.body_ratio_min,
        )
        assert out.iloc[0] == True

    def test_strong_close_rejects_doji(self, cfg):
        cf = cfg.trend_pullback.confirmation_5m
        df = pd.DataFrame({
            "open": [10.0], "high": [11.0], "low": [9.0], "close": [10.05],
        })
        out = ind.strong_close(
            df["open"], df["high"], df["low"], df["close"],
            close_position_min=cf.strong_close.close_position_min,
            body_ratio_min=cf.strong_close.body_ratio_min,
        )
        assert out.iloc[0] == False

    def test_weak_close_detects_textbook_bearish(self, cfg):
        cf = cfg.trend_pullback.confirmation_5m
        df = pd.DataFrame({
            "open": [19.0], "high": [20.0], "low": [9.0], "close": [10.0],
        })
        out = ind.weak_close(
            df["open"], df["high"], df["low"], df["close"],
            close_position_min=cf.strong_close.close_position_min,
            body_ratio_min=cf.strong_close.body_ratio_min,
        )
        assert out.iloc[0] == True

    def test_bullish_engulfing_basic(self, cfg):
        cf = cfg.trend_pullback.confirmation_5m
        # bar1: bear (open=10, close=9, body=1)
        # bar2: bull, opens below prev close, closes above prev open
        #       (open=8, close=12, body=4)
        # 4 > 1 * 1.1 → engulfing
        df = pd.DataFrame({
            "open":  [10.0, 8.0],
            "close": [9.0, 12.0],
        })
        out = ind.bullish_engulfing(
            df["open"], df["close"],
            body_growth_min=cf.engulfing.body_growth_min,
        )
        assert out.iloc[1] == True

    def test_bearish_engulfing_basic(self, cfg):
        cf = cfg.trend_pullback.confirmation_5m
        # bar1: bull (open=9, close=10)
        # bar2: bear, opens above prev close, closes below prev open
        df = pd.DataFrame({
            "open":  [9.0, 12.0],
            "close": [10.0, 8.0],
        })
        out = ind.bearish_engulfing(
            df["open"], df["close"],
            body_growth_min=cf.engulfing.body_growth_min,
        )
        assert out.iloc[1] == True

    def test_hammer_textbook(self):
        # body small at top, long lower shadow, no upper shadow
        # open=10, close=10.1, high=10.15, low=8.0
        # range = 2.15, body = 0.1, body/range ≈ 0.047 ≤ 0.3
        # upper_shadow = 10.15 - 10.1 = 0.05
        # lower_shadow = 10.0 - 8.0 = 2.0  (close < open? no, close > open → use open=10)
        #   實作中 lower_shadow = min(open, close) - low = 10 - 8 = 2.0
        df = pd.DataFrame({
            "open": [10.0], "high": [10.15], "low": [8.0], "close": [10.1],
        })
        out = ind.hammer(df["open"], df["high"], df["low"], df["close"])
        assert out.iloc[0] == True

    def test_shooting_star_textbook(self):
        df = pd.DataFrame({
            "open": [10.1], "high": [12.0], "low": [9.95], "close": [10.0],
        })
        out = ind.shooting_star(df["open"], df["high"], df["low"], df["close"])
        assert out.iloc[0] == True


# ---------------------------------------------------------------
# attach_core_indicators
# ---------------------------------------------------------------
class TestAttachCoreIndicators:
    def test_1h_attaches_all_required_columns(self, cfg):
        df = _make_clean_ohlcv(100, "1h", seed=4)
        out = ind.attach_core_indicators(df, cfg, "1h")
        for col in ["ema_20", "ema_50", "rsi_14", "atr_14", "atr_pct_14",
                    "bb_upper", "bb_middle", "bb_lower", "bb_width", "vwap"]:
            assert col in out.columns, f"missing {col}"

    def test_4h_uses_config_periods_not_hardcoded(self, cfg):
        df = _make_clean_ohlcv(300, "4h", seed=5)
        out = ind.attach_core_indicators(df, cfg, "4h")
        tfi = cfg.regime.trend_filter_indicators
        assert f"ema_{tfi.ema_short_period}" in out.columns
        assert f"ema_{tfi.ema_long_period}" in out.columns
        assert f"adx_{tfi.adx_period}" in out.columns

    def test_5m_attaches_pivot_with_tight_trailing_settings(self, cfg):
        df = _make_clean_ohlcv(60, "5m", seed=6)
        out = ind.attach_core_indicators(df, cfg, "5m")
        assert "pivot_high_5m_confirmed" in out.columns
        assert "pivot_low_5m_confirmed" in out.columns

    def test_empty_df_returns_empty(self, cfg):
        df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        out = ind.attach_core_indicators(df, cfg, "1h")
        assert out.empty

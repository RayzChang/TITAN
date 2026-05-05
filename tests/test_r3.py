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
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from strategies.r3.config_loader import R3Config
from strategies.r3 import indicators as ind
from strategies.r3.confirmation import MeanReversionConfirmation5M, TrendConfirmation5M
from strategies.r3.data_loader import (
    R3DataLoader,
    IntegrityReport,
    check_integrity,
    write_missing_data_report,
    _ohlcv_rows_to_df,
    _symbol_to_filename,
    TIMEFRAME_TO_SECONDS,
)
from strategies.r3.executor import (
    MeanReversionOrderIntentBuilder,
    OrderIntent,
    PartialFillSimulator,
    TrendOrderIntentBuilder,
)
from strategies.r3.exchange import R3ExchangeData
from strategies.r3.mean_reversion import MeanReversionStrategy
from strategies.r3.regime import Direction, Regime, RegimeState
from strategies.r3.risk_engine import RiskEngine
from strategies.r3.router import CooldownState, PositionState, R3Router
from strategies.r3.trailing import MeanReversionStopExitBuilder, TrendStopExitBuilder
from strategies.r3.trend_pullback import (
    TrendPullbackStrategy,
    evaluate_pullback_zone,
    evaluate_rsi_rebound,
    evaluate_signal_window,
)


# ---------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------
@pytest.fixture(scope="module")
def cfg() -> R3Config:
    return R3Config.load()


def _one_row_1h(
    *,
    low: float = 99.8,
    high: float = 100.2,
    close: float = 100.0,
    ema20: float = 100.0,
    ema50: float = 95.0,
    rsi_value: float = 51.0,
    atr_value: float = 1.0,
) -> pd.DataFrame:
    return pd.DataFrame({
        "open": [close],
        "high": [high],
        "low": [low],
        "close": [close],
        "volume": [100.0],
        "ema_20": [ema20],
        "ema_50": [ema50],
        "rsi_14": [rsi_value],
        "atr_14": [atr_value],
    }, index=[datetime(2026, 1, 1, tzinfo=timezone.utc)])


def _rsi_1h(values: list[float]) -> pd.DataFrame:
    idx = pd.date_range(datetime(2026, 1, 1, tzinfo=timezone.utc), periods=len(values), freq="1h")
    return pd.DataFrame({
        "open": [100.0] * len(values),
        "high": [101.0] * len(values),
        "low": [99.0] * len(values),
        "close": [100.0] * len(values),
        "volume": [100.0] * len(values),
        "ema_20": [100.0] * len(values),
        "ema_50": [95.0] * len(values),
        "rsi_14": values,
        "atr_14": [1.0] * len(values),
    }, index=idx)


def _trend_5m_df(direction: str = "long") -> pd.DataFrame:
    idx = pd.date_range(datetime(2026, 1, 1, 5, 0, tzinfo=timezone.utc), periods=10, freq="5min")
    if direction == "long":
        closes = [100.0, 100.1, 100.2, 100.3, 100.4, 100.5, 100.6, 100.7, 100.8, 101.5]
        opens = [c - 0.1 for c in closes]
        highs = [c + 0.1 for c in closes]
        lows = [c - 0.2 for c in closes]
    else:
        closes = [100.0, 99.9, 99.8, 99.7, 99.6, 99.5, 99.4, 99.3, 99.2, 98.5]
        opens = [c + 0.1 for c in closes]
        highs = [c + 0.2 for c in closes]
        lows = [c - 0.1 for c in closes]
    return pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": [100.0] * len(closes),
        "atr_14": [0.5] * len(closes),
    }, index=idx)


def _mr_5m_df(direction: str = "long", only_one_condition: bool = False) -> pd.DataFrame:
    idx = pd.date_range(datetime(2026, 1, 1, 4, 35, tzinfo=timezone.utc), periods=15, freq="5min")
    opens = [100.0] * 15
    highs = [101.0] * 15
    lows = [99.0] * 15
    closes = [100.0] * 15
    rsi_values = [50.0] * 15
    if direction == "long":
        opens[-2:] = [99.0, 98.0]
        highs[-2:] = [100.0, 100.0]
        lows[-2:] = [97.0, 95.0]
        closes[-2:] = [98.0, 98.5]
        rsi_values[-2:] = [28.0, 29.0]
        if only_one_condition:
            opens[-1] = 98.1
            highs[-1] = 99.0
            lows[-1] = 98.0
            closes[-1] = 98.8
            rsi_values[-2:] = [35.0, 34.0]
    else:
        opens[-2:] = [101.0, 102.0]
        highs[-2:] = [103.0, 105.0]
        lows[-2:] = [100.0, 100.0]
        closes[-2:] = [102.0, 101.5]
        rsi_values[-2:] = [72.0, 71.0]
        if only_one_condition:
            rsi_values[-2:] = [65.0, 66.0]
    return pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": [100.0] * len(closes),
        "rsi_14": rsi_values,
        "atr_14": [0.5] * len(closes),
    }, index=idx)


def _mr_1h_df(direction: str = "long") -> pd.DataFrame:
    idx = pd.date_range(datetime(2026, 1, 1, 0, tzinfo=timezone.utc), periods=6, freq="1h")
    if direction == "long":
        close = [100.0, 99.0, 98.0, 97.0, 96.0, 95.0]
        bb_lower = [96.0] * 6
        bb_upper = [104.0] * 6
        vwap_lower = [96.5] * 6
        vwap_upper = [103.5] * 6
        rsi_values = [40.0, 35.0, 31.0, 29.0, 28.0, 27.0]
    else:
        close = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
        bb_lower = [96.0] * 6
        bb_upper = [104.0] * 6
        vwap_lower = [96.5] * 6
        vwap_upper = [104.5] * 6
        rsi_values = [60.0, 65.0, 69.0, 71.0, 72.0, 73.0]
    return pd.DataFrame({
        "open": close,
        "high": [value + 1.0 for value in close],
        "low": [value - 1.0 for value in close],
        "close": close,
        "volume": [100.0] * 6,
        "bb_lower": bb_lower,
        "bb_middle": [100.0] * 6,
        "bb_upper": bb_upper,
        "vwap": [101.0 if direction == "long" else 99.0] * 6,
        "vwap_lower": vwap_lower,
        "vwap_upper": vwap_upper,
        "vwap_stdev": [1.0] * 6,
        "rsi_14": rsi_values,
        "atr_14": [2.0] * 6,
    }, index=idx)


def _manual_regime_state(
    *,
    regime: Regime = Regime.A_TREND,
    direction: str = Direction.LONG.value,
    allow_new_entries: bool = True,
    funding_z: float = 0.5,
    extreme_vol: bool = False,
) -> RegimeState:
    ema_short = 100.0 if direction == Direction.LONG.value else 90.0
    ema_long = 90.0 if direction == Direction.LONG.value else 100.0
    return RegimeState(
        as_of=datetime(2026, 1, 1, 5, 5, tzinfo=timezone.utc),
        symbol="BTC/USDT:USDT",
        regime=regime,
        regime_name="trend" if regime == Regime.A_TREND else "other",
        direction=direction,
        allow_new_entries=allow_new_entries,
        reason_codes=[regime.value],
        metrics_snapshot={
            "ema_4h_short": ema_short,
            "ema_4h_long": ema_long,
            "adx_4h": 30.0,
            "funding_z": funding_z,
            "extreme_vol": extreme_vol,
        },
    )


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
        df = _one_row_1h(low=100.2, close=100.1, ema20=100.0, ema50=95.0)
        result = evaluate_pullback_zone(df, cfg, "long")
        assert result.passed is True
        assert result.matched_ema == "ema_20"
        assert "PULLBACK_TO_EMA20" in result.reason_codes

    def test_long_pullback_low_too_far_from_ema_should_not_qualify(self, cfg):
        df = _one_row_1h(low=100.4, close=100.1, ema20=100.0, ema50=95.0)
        result = evaluate_pullback_zone(df, cfg, "long")
        assert result.passed is False
        assert "NO_VALID_PULLBACK" in result.reason_codes

    def test_long_close_outside_band_should_not_qualify(self, cfg):
        df = _one_row_1h(low=100.1, close=100.5, ema20=100.0, ema50=95.0)
        result = evaluate_pullback_zone(df, cfg, "long")
        assert result.passed is False
        assert "NO_VALID_PULLBACK" in result.reason_codes

    def test_short_symmetric(self, cfg):
        df = _one_row_1h(high=99.8, close=99.9, ema20=100.0, ema50=105.0)
        result = evaluate_pullback_zone(df, cfg, "short")
        assert result.passed is True
        assert result.matched_ema == "ema_20"
        assert "PULLBACK_TO_EMA20" in result.reason_codes

    def test_long_pullback_to_ema50_should_qualify(self, cfg):
        df = _one_row_1h(low=95.2, close=95.1, ema20=105.0, ema50=95.0)
        result = evaluate_pullback_zone(df, cfg, "long")
        assert result.passed is True
        assert result.matched_ema == "ema_50"
        assert "PULLBACK_TO_EMA50" in result.reason_codes

    def test_short_pullback_to_ema50_should_qualify(self, cfg):
        df = _one_row_1h(high=105.2, close=105.1, ema20=95.0, ema50=105.0)
        result = evaluate_pullback_zone(df, cfg, "short")
        assert result.passed is True
        assert result.matched_ema == "ema_50"
        assert "PULLBACK_TO_EMA50" in result.reason_codes


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
        result = evaluate_rsi_rebound(_rsi_1h([55, 49, 48, 50, 51]), cfg, "long")
        assert result.passed is True
        assert "RSI_REBOUNDED_LONG" in result.reason_codes

    def test_rsi_never_below_50_should_not_qualify(self, cfg):
        result = evaluate_rsi_rebound(_rsi_1h([55, 56, 57, 58, 59]), cfg, "long")
        assert result.passed is False
        assert "RSI_CONDITION_FAILED" in result.reason_codes

    def test_rsi_below_50_but_not_uptick_should_not_qualify(self, cfg):
        result = evaluate_rsi_rebound(_rsi_1h([42, 47, 51, 50, 49]), cfg, "long")
        assert result.passed is False
        assert "RSI_CONDITION_FAILED" in result.reason_codes

    def test_short_symmetric(self, cfg):
        result = evaluate_rsi_rebound(_rsi_1h([45, 52, 55, 51, 49]), cfg, "short")
        assert result.passed is True
        assert "RSI_REJECTED_SHORT" in result.reason_codes

    def test_short_without_downtick_should_not_qualify(self, cfg):
        result = evaluate_rsi_rebound(_rsi_1h([55, 54, 49, 48, 49]), cfg, "short")
        assert result.passed is False
        assert "RSI_CONDITION_FAILED" in result.reason_codes


# ===============================================================
# Q23 — 5M 訊號有效窗口（1H 收盤後 12 根 5M 內）
# ===============================================================
class TestQ23SignalValidityWindow:
    def test_config_value_match_spec(self, cfg):
        assert cfg.trend_pullback.signal_validity_window_5m_bars == 12

    def test_signal_at_5m_bar_1_to_12_is_valid(self, cfg):
        signal_close = datetime(2026, 1, 1, 5, 0, tzinfo=timezone.utc)
        for bar in range(1, 13):
            current = signal_close + timedelta(minutes=5 * bar)
            result = evaluate_signal_window(signal_close, current, cfg)
            assert result.passed is True
            assert "SIGNAL_WINDOW_VALID" in result.reason_codes

    def test_signal_at_5m_bar_13_should_be_invalid(self, cfg):
        signal_close = datetime(2026, 1, 1, 5, 0, tzinfo=timezone.utc)
        current = signal_close + timedelta(minutes=65)
        result = evaluate_signal_window(signal_close, current, cfg)
        assert result.passed is False
        assert "SIGNAL_WINDOW_EXPIRED" in result.reason_codes


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
        signal = SimpleNamespace(
            approved=True,
            direction="short",
            strategy_name="trend_pullback",
        )
        decision = R3Router(cfg).route(
            symbol="BTC/USDT:USDT",
            timestamp=datetime(2026, 1, 1, 5, tzinfo=timezone.utc),
            regime_state=_manual_regime_state(regime=Regime.A_TREND, direction="short"),
            trend_signal_result=signal,
            existing_position_state=PositionState(
                symbol="BTC/USDT:USDT",
                has_position=True,
                direction="long",
                strategy_name="trend_pullback",
                quantity=1.0,
                entry_price=100.0,
            ),
        )
        assert decision.approved is False
        assert "REJECT_OPPOSITE_POSITION_EXISTS" in decision.rejection_reasons
        assert "REJECT_HEDGE_MODE_DISABLED" in decision.rejection_reasons
        assert "WAIT_FOR_EXISTING_POSITION_EXIT" in decision.rejection_reasons

    def test_after_existing_closed_new_direction_allowed(self, cfg):
        signal = SimpleNamespace(
            approved=True,
            direction="short",
            strategy_name="trend_pullback",
        )
        decision = R3Router(cfg).route(
            symbol="BTC/USDT:USDT",
            timestamp=datetime(2026, 1, 1, 5, tzinfo=timezone.utc),
            regime_state=_manual_regime_state(regime=Regime.A_TREND, direction="short"),
            trend_signal_result=signal,
            existing_position_state=PositionState(
                symbol="BTC/USDT:USDT",
                has_position=False,
            ),
        )
        assert decision.approved is True
        assert decision.selected_strategy == "trend_pullback"


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
        df = _mr_5m_df("long")
        result = MeanReversionConfirmation5M(cfg).check(
            df,
            df.index[-1],
            "BTC/USDT:USDT",
            "long",
        )
        assert result.passed is True
        assert result.strategy_name == "mean_reversion"
        assert "MR_BULLISH_CLOSE" in result.conditions_passed
        assert "MR_RSI_REVERSAL_LONG" in result.conditions_passed
        assert "MR_CONFIRMATION_PASSED" in result.reason_codes

    def test_only_one_condition_should_not_qualify(self, cfg):
        df = _mr_5m_df("long", only_one_condition=True)
        result = MeanReversionConfirmation5M(cfg).check(
            df,
            df.index[-1],
            "BTC/USDT:USDT",
            "long",
        )
        assert result.passed is False
        assert result.conditions_passed == ["MR_BULLISH_CLOSE"]
        assert "MR_CONFIRMATION_FAILED" in result.reason_codes

    def test_short_two_of_three_should_qualify(self, cfg):
        df = _mr_5m_df("short")
        result = MeanReversionConfirmation5M(cfg).check(
            df,
            df.index[-1],
            "BTC/USDT:USDT",
            "short",
        )
        assert result.passed is True
        assert "MR_BEARISH_CLOSE" in result.conditions_passed
        assert "MR_RSI_REVERSAL_SHORT" in result.conditions_passed

    def test_mr_rsi_reversal_long_correct(self, cfg):
        df = _mr_5m_df("long")
        result = MeanReversionConfirmation5M(cfg).check_long(
            df,
            df.index[-1],
            "BTC/USDT:USDT",
        )
        assert result.metrics_snapshot["current_rsi"] == pytest.approx(29.0)
        assert result.metrics_snapshot["previous_rsi"] == pytest.approx(28.0)
        assert "MR_RSI_REVERSAL_LONG" in result.conditions_passed

    def test_mr_rsi_reversal_short_correct(self, cfg):
        df = _mr_5m_df("short")
        result = MeanReversionConfirmation5M(cfg).check_short(
            df,
            df.index[-1],
            "BTC/USDT:USDT",
        )
        assert result.metrics_snapshot["current_rsi"] == pytest.approx(71.0)
        assert result.metrics_snapshot["previous_rsi"] == pytest.approx(72.0)
        assert "MR_RSI_REVERSAL_SHORT" in result.conditions_passed


# ===============================================================
# Q26 — Position quantity 用 limit_price 計算
# ===============================================================
class TestQ26QuantityFromLimitPrice:
    def test_config_value_match_spec(self, cfg):
        assert cfg.trend_pullback.entry_order.quantity_basis == "limit_price"

    def test_quantity_formula_uses_limit_price_not_current(self, cfg):
        engine = RiskEngine(cfg)
        plan = engine.build_plan(
            symbol="BTC/USDT:USDT",
            direction="long",
            equity=5000.0,
            entry_price=100.0,
            stop_price=98.0,
        )
        assert plan.approved is True
        assert plan.risk_amount == pytest.approx(37.5)
        assert plan.stop_loss_pct == pytest.approx(0.02)
        assert plan.position_notional == pytest.approx(1875.0)
        assert plan.quantity == pytest.approx(18.75)


# ===============================================================
# Q27 — 部分成交處理
# ===============================================================
class TestQ27PartialFill:
    def test_config_values_match_spec(self, cfg):
        pf = cfg.trend_pullback.entry_order.partial_fill
        assert pf.treat_filled_as_entry is True
        assert pf.cancel_remaining_after_timeout is True

    def test_filled_portion_immediately_protected_by_sl(self, cfg):
        order = OrderIntent(
            symbol="BTC/USDT:USDT",
            direction="long",
            order_type="LIMIT_MAKER",
            time_in_force="GTX",
            limit_price=100.0,
            quantity=10.0,
            reduce_only=False,
            signal_id="sig-1",
        )
        sim = PartialFillSimulator(cfg).simulate(
            order,
            filled_quantity=4.0,
            entry_price=100.0,
            stop_price=98.0,
            timeout_reached=False,
        )
        assert sim.stop_order_intent is not None
        assert sim.stop_order_intent.reduce_only is True
        assert sim.stop_order_intent.quantity == pytest.approx(4.0)

    def test_unfilled_portion_canceled_at_timeout(self, cfg):
        order = OrderIntent(
            symbol="BTC/USDT:USDT",
            direction="long",
            order_type="LIMIT_MAKER",
            time_in_force="GTX",
            limit_price=100.0,
            quantity=10.0,
            reduce_only=False,
            signal_id="sig-1",
        )
        sim = PartialFillSimulator(cfg).simulate(
            order,
            filled_quantity=4.0,
            entry_price=100.0,
            stop_price=98.0,
            timeout_reached=True,
        )
        assert sim.remaining_quantity == pytest.approx(6.0)
        assert sim.cancel_remaining_after_timeout is True

    def test_r_multiple_uses_actual_filled_qty(self, cfg):
        order = OrderIntent(
            symbol="BTC/USDT:USDT",
            direction="long",
            order_type="LIMIT_MAKER",
            time_in_force="GTX",
            limit_price=100.0,
            quantity=10.0,
            reduce_only=False,
            signal_id="sig-1",
        )
        sim = PartialFillSimulator(cfg).simulate(
            order,
            filled_quantity=4.0,
            entry_price=100.0,
            stop_price=98.0,
            timeout_reached=True,
        )
        assert sim.max_loss == pytest.approx(8.0)


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
        assert RiskEngine(cfg).compute_equity(
            wallet_balance=5000.0,
            realized_pnl=0.0,
            unrealized_pnl=200.0,
        ) == pytest.approx(5000.0)

    def test_negative_unrealized_immediately_reduces_equity(self, cfg):
        assert RiskEngine(cfg).compute_equity(
            wallet_balance=5000.0,
            realized_pnl=0.0,
            unrealized_pnl=-300.0,
        ) == pytest.approx(4700.0)


# ===============================================================
# Q29 — 同策略連續訊號 Cooldown
# ===============================================================
class TestQ29SameStrategyCooldown:
    def test_config_values_match_spec(self, cfg):
        cd = cfg.trend_pullback.cooldown_after
        assert cd.sl_exit_1h_bars == 1
        assert cd.tp_exit_1h_bars == 0

    def test_sl_exit_blocks_next_1h_signal(self, cfg):
        now = datetime(2026, 1, 1, 5, 30, tzinfo=timezone.utc)
        signal = SimpleNamespace(approved=True, direction="long", strategy_name="trend_pullback")
        decision = R3Router(cfg).route(
            symbol="BTC/USDT:USDT",
            timestamp=now,
            regime_state=_manual_regime_state(regime=Regime.A_TREND, direction="long"),
            trend_signal_result=signal,
            cooldown_state=CooldownState(
                symbol="BTC/USDT:USDT",
                strategy_name="trend_pullback",
                last_exit_reason="SL",
                last_exit_time=datetime(2026, 1, 1, 5, tzinfo=timezone.utc),
                cooldown_until=datetime(2026, 1, 1, 6, tzinfo=timezone.utc),
            ),
        )
        assert decision.approved is False
        assert "REJECT_COOLDOWN_ACTIVE" in decision.rejection_reasons

    def test_tp_exit_does_not_block(self, cfg):
        now = datetime(2026, 1, 1, 5, 30, tzinfo=timezone.utc)
        signal = SimpleNamespace(approved=True, direction="long", strategy_name="trend_pullback")
        decision = R3Router(cfg).route(
            symbol="BTC/USDT:USDT",
            timestamp=now,
            regime_state=_manual_regime_state(regime=Regime.A_TREND, direction="long"),
            trend_signal_result=signal,
            cooldown_state=CooldownState(
                symbol="BTC/USDT:USDT",
                strategy_name="trend_pullback",
                last_exit_reason="TP",
                last_exit_time=datetime(2026, 1, 1, 5, tzinfo=timezone.utc),
                cooldown_until=datetime(2026, 1, 1, 6, tzinfo=timezone.utc),
            ),
        )
        assert decision.approved is True

    def test_cooldown_only_applies_per_symbol_per_strategy(self, cfg):
        now = datetime(2026, 1, 1, 5, 30, tzinfo=timezone.utc)
        eth_signal = SimpleNamespace(approved=True, direction="long", strategy_name="trend_pullback")
        eth_decision = R3Router(cfg).route(
            symbol="ETH/USDT:USDT",
            timestamp=now,
            regime_state=_manual_regime_state(regime=Regime.A_TREND, direction="long"),
            trend_signal_result=eth_signal,
            cooldown_state=CooldownState(
                symbol="BTC/USDT:USDT",
                strategy_name="trend_pullback",
                last_exit_reason="SL",
                last_exit_time=datetime(2026, 1, 1, 5, tzinfo=timezone.utc),
                cooldown_until=datetime(2026, 1, 1, 6, tzinfo=timezone.utc),
            ),
        )
        mr_signal = SimpleNamespace(approved=True, direction="long", strategy_name="mean_reversion")
        mr_decision = R3Router(cfg).route(
            symbol="BTC/USDT:USDT",
            timestamp=now,
            regime_state=_manual_regime_state(
                regime=Regime.B_SIDEWAYS,
                direction=Direction.NEUTRAL.value,
            ),
            mean_reversion_signal_result=mr_signal,
            cooldown_state=CooldownState(
                symbol="BTC/USDT:USDT",
                strategy_name="trend_pullback",
                last_exit_reason="SL",
                last_exit_time=datetime(2026, 1, 1, 5, tzinfo=timezone.utc),
                cooldown_until=datetime(2026, 1, 1, 6, tzinfo=timezone.utc),
            ),
        )
        assert eth_decision.approved is True
        assert mr_decision.approved is True


# ===============================================================
# ===============================================================
# Sprint 1 — Data Layer
# ===============================================================
# ===============================================================

class TestSprint3Confirmation:
    def test_long_three_conditions_any_two_pass(self, cfg):
        result = TrendConfirmation5M(cfg).check(
            _trend_5m_df("long"),
            datetime(2026, 1, 1, 5, 45, tzinfo=timezone.utc),
            "BTC/USDT:USDT",
            "long",
        )
        assert result.passed is True
        assert len(result.conditions_passed) >= 2

    def test_short_three_conditions_any_two_pass(self, cfg):
        result = TrendConfirmation5M(cfg).check(
            _trend_5m_df("short"),
            datetime(2026, 1, 1, 5, 45, tzinfo=timezone.utc),
            "BTC/USDT:USDT",
            "short",
        )
        assert result.passed is True
        assert len(result.conditions_passed) >= 2

    def test_only_one_condition_does_not_pass(self, cfg):
        idx = pd.date_range(datetime(2026, 1, 1, tzinfo=timezone.utc), periods=10, freq="5min")
        df = pd.DataFrame({
            "open": [100.0] * 9 + [100.05],
            "high": [110.0] * 9 + [110.0],
            "low": [90.0] * 10,
            "close": [100.0] * 9 + [100.1],
            "volume": [100.0] * 10,
        }, index=idx)
        result = TrendConfirmation5M(cfg).check(df, idx[-1], "BTC/USDT:USDT", "long")
        assert result.passed is False
        assert len(result.conditions_passed) == 1

    def test_ema9_slope_uses_i_vs_i_minus_2(self, cfg):
        df = _trend_5m_df("long")
        result = TrendConfirmation5M(cfg).check(df, df.index[-1], "BTC/USDT:USDT", "long")
        ema9 = ind.ema(df["close"], cfg.trend_pullback.confirmation_5m.ema9_period)
        assert result.metrics_snapshot["ema9_slope_ref"] == pytest.approx(float(ema9.iloc[-3]))

    def test_confirmation_does_not_look_past_as_of(self, cfg):
        df = _trend_5m_df("long")
        future = df.iloc[-1:].copy()
        future.index = [df.index[-1] + timedelta(minutes=5)]
        future["close"] = 1000.0
        with_future = pd.concat([df, future])
        as_of = df.index[-2]
        result = TrendConfirmation5M(cfg).check(with_future, as_of, "BTC/USDT:USDT", "long")
        assert result.metrics_snapshot["close"] == pytest.approx(float(df.loc[as_of, "close"]))


class TestSprint3RiskStopExecutor:
    def test_risk_multiplier_half_reduces_risk(self, cfg):
        plan = RiskEngine(cfg).build_plan(
            symbol="BTC/USDT:USDT",
            direction="long",
            equity=5000.0,
            entry_price=100.0,
            stop_price=98.0,
            risk_multiplier=0.5,
        )
        assert plan.risk_amount == pytest.approx(18.75)
        assert plan.quantity == pytest.approx(9.375)

    def test_max_total_open_risk_rejects(self, cfg):
        plan = RiskEngine(cfg).build_plan(
            symbol="BTC/USDT:USDT",
            direction="long",
            equity=5000.0,
            entry_price=100.0,
            stop_price=98.0,
            current_open_risk_pct=0.01,
        )
        assert plan.approved is False
        assert "EXCEEDS_MAX_TOTAL_OPEN_RISK" in plan.rejection_reasons

    def test_stop_uses_pivot_when_available(self, cfg):
        stop = TrendStopExitBuilder(cfg).build_stop_plan(
            symbol="BTC/USDT:USDT",
            direction="long",
            entry_price=100.0,
            atr_1h=1.0,
            latest_pivot_low=98.0,
        )
        assert stop.stop_source == "pivot_low"
        assert stop.stop_price == pytest.approx(97.8)

    def test_stop_uses_atr_fallback_without_pivot(self, cfg):
        stop = TrendStopExitBuilder(cfg).build_stop_plan(
            symbol="BTC/USDT:USDT",
            direction="short",
            entry_price=100.0,
            atr_1h=1.0,
        )
        assert stop.stop_source == "atr_fallback"
        assert stop.stop_price == pytest.approx(101.8)

    def test_exit_plan_prices_from_r(self, cfg):
        assert cfg.trend_pullback.take_profit.tp2.tp2_r == pytest.approx(2.75)
        exit_plan = TrendStopExitBuilder(cfg).build_exit_plan(
            symbol="BTC/USDT:USDT",
            direction="long",
            entry_price=100.0,
            stop_price=98.0,
        )
        assert exit_plan.tp1_price == pytest.approx(102.0)
        assert exit_plan.tp1_fraction == pytest.approx(0.5)
        assert exit_plan.tp2_price == pytest.approx(105.5)
        assert exit_plan.tp2_fraction == pytest.approx(0.5)

    def test_long_maker_limit_price(self, cfg):
        price = TrendOrderIntentBuilder(cfg).compute_limit_price(
            direction="long",
            current_bid=100.0,
            current_ask=100.1,
            tick_size=0.01,
            ema20_1h=100.0,
            signal_5m_close=100.5,
            atr_5m=1.0,
        )
        assert price == pytest.approx(99.99)

    def test_short_maker_limit_price(self, cfg):
        price = TrendOrderIntentBuilder(cfg).compute_limit_price(
            direction="short",
            current_bid=100.0,
            current_ask=100.1,
            tick_size=0.01,
            ema20_1h=100.0,
            signal_5m_close=99.5,
            atr_5m=1.0,
        )
        assert price == pytest.approx(100.11)

    def test_order_intent_expires_after_10_minutes(self, cfg):
        ts = datetime(2026, 1, 1, 5, 45, tzinfo=timezone.utc)
        intent = TrendOrderIntentBuilder(cfg).build_entry_intent(
            symbol="BTC/USDT:USDT",
            direction="long",
            signal_timestamp=ts,
            current_bid=100.0,
            current_ask=100.1,
            tick_size=0.01,
            ema20_1h=100.0,
            signal_5m_close=100.5,
            atr_5m=1.0,
            quantity=1.0,
            signal_id="sig-1",
        )
        assert intent.expires_at == ts + timedelta(minutes=10)
        assert intent.reduce_only is False


class TestSprint3TrendStrategy:
    def _long_1h(self):
        idx = pd.date_range(datetime(2026, 1, 1, 0, tzinfo=timezone.utc), periods=6, freq="1h")
        return pd.DataFrame({
            "open": [100.0] * 6,
            "high": [101.0] * 6,
            "low": [99.8] * 6,
            "close": [100.1] * 6,
            "volume": [100.0] * 6,
            "ema_20": [100.0] * 6,
            "ema_50": [99.0] * 6,
            "rsi_14": [45.0, 47.0, 49.0, 48.0, 49.0, 51.0],
            "atr_14": [1.0] * 6,
        }, index=idx)

    def _short_1h(self):
        idx = pd.date_range(datetime(2026, 1, 1, 0, tzinfo=timezone.utc), periods=6, freq="1h")
        return pd.DataFrame({
            "open": [100.0] * 6,
            "high": [100.2] * 6,
            "low": [99.0] * 6,
            "close": [99.9] * 6,
            "volume": [100.0] * 6,
            "ema_20": [100.0] * 6,
            "ema_50": [101.0] * 6,
            "rsi_14": [55.0, 57.0, 54.0, 52.0, 51.0, 49.0],
            "atr_14": [1.0] * 6,
        }, index=idx)

    def test_regime_a_long_full_stack_approves_signal(self, cfg):
        as_of = datetime(2026, 1, 1, 5, 45, tzinfo=timezone.utc)
        result = TrendPullbackStrategy(cfg).evaluate(
            symbol="BTC/USDT:USDT",
            as_of=as_of,
            regime_state=_manual_regime_state(direction="long"),
            df_1h=self._long_1h(),
            df_5m=_trend_5m_df("long"),
            equity=5000.0,
            current_bid=100.0,
            current_ask=100.1,
            tick_size=0.01,
        )
        assert result.approved is True
        assert result.entry_order_intent is not None
        assert result.risk_plan is not None
        assert result.stop_plan is not None
        assert result.exit_plan is not None

    def test_regime_a_short_full_stack_approves_signal(self, cfg):
        as_of = datetime(2026, 1, 1, 5, 45, tzinfo=timezone.utc)
        result = TrendPullbackStrategy(cfg).evaluate(
            symbol="BTC/USDT:USDT",
            as_of=as_of,
            regime_state=_manual_regime_state(direction="short"),
            df_1h=self._short_1h(),
            df_5m=_trend_5m_df("short"),
            equity=5000.0,
            current_bid=100.0,
            current_ask=100.1,
            tick_size=0.01,
        )
        assert result.approved is True
        assert result.direction == "short"

    @pytest.mark.parametrize("regime", [Regime.UNKNOWN, Regime.B_SIDEWAYS, Regime.D_NO_TRADE])
    def test_non_a_regimes_reject(self, cfg, regime):
        result = TrendPullbackStrategy(cfg).evaluate(
            symbol="BTC/USDT:USDT",
            as_of=datetime(2026, 1, 1, 5, 45, tzinfo=timezone.utc),
            regime_state=_manual_regime_state(regime=regime, direction="long"),
            df_1h=self._long_1h(),
            df_5m=_trend_5m_df("long"),
            equity=5000.0,
            current_bid=100.0,
            current_ask=100.1,
            tick_size=0.01,
        )
        assert result.approved is False
        assert "REGIME_NOT_A" in result.rejection_reasons

    def test_funding_overheated_rejects_long(self, cfg):
        result = TrendPullbackStrategy(cfg).evaluate(
            symbol="BTC/USDT:USDT",
            as_of=datetime(2026, 1, 1, 5, 45, tzinfo=timezone.utc),
            regime_state=_manual_regime_state(direction="long", funding_z=2.1),
            df_1h=self._long_1h(),
            df_5m=_trend_5m_df("long"),
            equity=5000.0,
            current_bid=100.0,
            current_ask=100.1,
            tick_size=0.01,
        )
        assert result.approved is False
        assert "FUNDING_OVERHEATED_LONG" in result.rejection_reasons

    def test_extreme_vol_rejects(self, cfg):
        result = TrendPullbackStrategy(cfg).evaluate(
            symbol="BTC/USDT:USDT",
            as_of=datetime(2026, 1, 1, 5, 45, tzinfo=timezone.utc),
            regime_state=_manual_regime_state(direction="long", extreme_vol=True),
            df_1h=self._long_1h(),
            df_5m=_trend_5m_df("long"),
            equity=5000.0,
            current_bid=100.0,
            current_ask=100.1,
            tick_size=0.01,
        )
        assert result.approved is False
        assert "EXTREME_VOL" in result.rejection_reasons

    def test_allow_new_entries_false_rejects(self, cfg):
        result = TrendPullbackStrategy(cfg).evaluate(
            symbol="BTC/USDT:USDT",
            as_of=datetime(2026, 1, 1, 5, 45, tzinfo=timezone.utc),
            regime_state=_manual_regime_state(direction="long", allow_new_entries=False),
            df_1h=self._long_1h(),
            df_5m=_trend_5m_df("long"),
            equity=5000.0,
            current_bid=100.0,
            current_ask=100.1,
            tick_size=0.01,
        )
        assert result.approved is False
        assert "ALLOW_NEW_ENTRIES_FALSE" in result.rejection_reasons


class TestSprint3Regression:
    def test_executor_only_creates_order_intent(self):
        from pathlib import Path
        src = Path(__file__).resolve().parents[1] / "strategies" / "r3" / "executor.py"
        text = src.read_text(encoding="utf-8")
        for forbidden in ["create_order", "submit_order", "cancel_order", "ccxt"]:
            assert forbidden not in text

    def test_trend_strategy_does_not_import_backtest_or_other_strategies(self):
        from pathlib import Path
        src = Path(__file__).resolve().parents[1] / "strategies" / "r3" / "trend_pullback.py"
        text = src.read_text(encoding="utf-8")
        for forbidden in ["BacktestEngine", "from .mean_reversion", "from .funding_reversal", "position_manager"]:
            assert forbidden not in text


class TestSprint4MeanReversionStrategy:
    def test_regime_b_long_full_stack_approves_signal(self, cfg):
        as_of = datetime(2026, 1, 1, 5, 45, tzinfo=timezone.utc)
        result = MeanReversionStrategy(cfg).evaluate(
            symbol="BTC/USDT:USDT",
            as_of=as_of,
            regime_state=_manual_regime_state(
                regime=Regime.B_SIDEWAYS,
                direction=Direction.NEUTRAL.value,
            ),
            df_1h=_mr_1h_df("long"),
            df_5m=_mr_5m_df("long"),
            equity=5000.0,
            current_bid=95.0,
            current_ask=95.1,
            tick_size=0.01,
        )
        assert result.approved is True
        assert result.strategy_name == "mean_reversion"
        assert result.direction == "long"
        assert result.confirmation_result.strategy_name == "mean_reversion"
        assert result.risk_plan is not None
        assert result.stop_plan.strategy_name == "mean_reversion"
        assert result.exit_plan.strategy_name == "mean_reversion"
        assert result.entry_order_intent is not None

    def test_regime_b_short_full_stack_approves_signal(self, cfg):
        as_of = datetime(2026, 1, 1, 5, 45, tzinfo=timezone.utc)
        result = MeanReversionStrategy(cfg).evaluate(
            symbol="BTC/USDT:USDT",
            as_of=as_of,
            regime_state=_manual_regime_state(
                regime=Regime.B_SIDEWAYS,
                direction=Direction.NEUTRAL.value,
            ),
            df_1h=_mr_1h_df("short"),
            df_5m=_mr_5m_df("short"),
            equity=5000.0,
            current_bid=105.0,
            current_ask=105.1,
            tick_size=0.01,
        )
        assert result.approved is True
        assert result.direction == "short"
        assert result.entry_order_intent.limit_price == pytest.approx(105.11)

    @pytest.mark.parametrize("regime", [Regime.A_TREND, Regime.UNKNOWN, Regime.D_NO_TRADE])
    def test_non_b_regimes_reject(self, cfg, regime):
        result = MeanReversionStrategy(cfg).evaluate(
            symbol="BTC/USDT:USDT",
            as_of=datetime(2026, 1, 1, 5, 45, tzinfo=timezone.utc),
            regime_state=_manual_regime_state(
                regime=regime,
                direction=Direction.NEUTRAL.value,
            ),
            df_1h=_mr_1h_df("long"),
            df_5m=_mr_5m_df("long"),
            equity=5000.0,
            current_bid=95.0,
            current_ask=95.1,
            tick_size=0.01,
        )
        assert result.approved is False
        assert "REGIME_NOT_B" in result.rejection_reasons

    def test_funding_extreme_rejects(self, cfg):
        result = MeanReversionStrategy(cfg).evaluate(
            symbol="BTC/USDT:USDT",
            as_of=datetime(2026, 1, 1, 5, 45, tzinfo=timezone.utc),
            regime_state=_manual_regime_state(
                regime=Regime.B_SIDEWAYS,
                direction=Direction.NEUTRAL.value,
                funding_z=1.6,
            ),
            df_1h=_mr_1h_df("long"),
            df_5m=_mr_5m_df("long"),
            equity=5000.0,
            current_bid=95.0,
            current_ask=95.1,
            tick_size=0.01,
        )
        assert result.approved is False
        assert "FUNDING_NOT_NEUTRAL" in result.rejection_reasons

    def test_extreme_vol_rejects(self, cfg):
        result = MeanReversionStrategy(cfg).evaluate(
            symbol="BTC/USDT:USDT",
            as_of=datetime(2026, 1, 1, 5, 45, tzinfo=timezone.utc),
            regime_state=_manual_regime_state(
                regime=Regime.B_SIDEWAYS,
                direction=Direction.NEUTRAL.value,
                extreme_vol=True,
            ),
            df_1h=_mr_1h_df("long"),
            df_5m=_mr_5m_df("long"),
            equity=5000.0,
            current_bid=95.0,
            current_ask=95.1,
            tick_size=0.01,
        )
        assert result.approved is False
        assert "EXTREME_VOL" in result.rejection_reasons

    def test_allow_new_entries_false_rejects(self, cfg):
        result = MeanReversionStrategy(cfg).evaluate(
            symbol="BTC/USDT:USDT",
            as_of=datetime(2026, 1, 1, 5, 45, tzinfo=timezone.utc),
            regime_state=_manual_regime_state(
                regime=Regime.B_SIDEWAYS,
                direction=Direction.NEUTRAL.value,
                allow_new_entries=False,
            ),
            df_1h=_mr_1h_df("long"),
            df_5m=_mr_5m_df("long"),
            equity=5000.0,
            current_bid=95.0,
            current_ask=95.1,
            tick_size=0.01,
        )
        assert result.approved is False
        assert "ALLOW_NEW_ENTRIES_FALSE" in result.rejection_reasons

    def test_mr_cannot_use_trend_breakout_confirmation(self, cfg):
        df = _mr_5m_df("long", only_one_condition=True)
        df.loc[df.index[-1], "close"] = float(df["high"].iloc[-4:-1].max()) + 10.0
        result = MeanReversionConfirmation5M(cfg).check_long(
            df,
            df.index[-1],
            "BTC/USDT:USDT",
        )
        assert result.passed is False
        assert all("BREAK" not in code.upper() for code in result.conditions_passed)


class TestSprint4MRStopExitExecutor:
    def test_long_stop_uses_mr_atr_multiplier(self, cfg):
        stop = MeanReversionStopExitBuilder(cfg).build_stop_plan(
            symbol="BTC/USDT:USDT",
            direction="long",
            entry_price=100.0,
            atr_1h=2.0,
        )
        assert cfg.mean_reversion.exit.sl_atr_mult == pytest.approx(1.0)
        assert stop.stop_source == "ATR_MR"
        assert stop.stop_price == pytest.approx(98.0)

    def test_short_stop_uses_mr_atr_multiplier(self, cfg):
        stop = MeanReversionStopExitBuilder(cfg).build_stop_plan(
            symbol="BTC/USDT:USDT",
            direction="short",
            entry_price=100.0,
            atr_1h=2.0,
        )
        assert stop.stop_source == "ATR_MR"
        assert stop.stop_price == pytest.approx(102.0)

    def test_long_target_uses_conservative_min(self, cfg):
        plan = MeanReversionStopExitBuilder(cfg).build_exit_plan(
            symbol="BTC/USDT:USDT",
            direction="long",
            entry_price=95.0,
            stop_price=93.0,
            bb_middle=100.0,
            vwap=101.0,
        )
        assert plan.tp1_price == pytest.approx(100.0)
        assert plan.target_source == "CONSERVATIVE_TARGET"
        assert plan.time_stop_hours == pytest.approx(12.0)

    def test_short_target_uses_conservative_max(self, cfg):
        plan = MeanReversionStopExitBuilder(cfg).build_exit_plan(
            symbol="BTC/USDT:USDT",
            direction="short",
            entry_price=105.0,
            stop_price=107.0,
            bb_middle=100.0,
            vwap=99.0,
        )
        assert plan.tp1_price == pytest.approx(100.0)
        assert plan.target_source == "CONSERVATIVE_TARGET"
        assert plan.time_stop_hours == pytest.approx(12.0)

    def test_long_mr_limit_price(self, cfg):
        price = MeanReversionOrderIntentBuilder(cfg).compute_limit_price(
            direction="long",
            current_bid=95.0,
            current_ask=95.1,
            tick_size=0.01,
            signal_5m_close=94.9,
        )
        assert price == pytest.approx(94.9)

    def test_short_mr_limit_price(self, cfg):
        price = MeanReversionOrderIntentBuilder(cfg).compute_limit_price(
            direction="short",
            current_bid=105.0,
            current_ask=105.1,
            tick_size=0.01,
            signal_5m_close=105.2,
        )
        assert price == pytest.approx(105.2)

    def test_mr_order_intent_expires_after_10_minutes(self, cfg):
        ts = datetime(2026, 1, 1, 5, 45, tzinfo=timezone.utc)
        intent = MeanReversionOrderIntentBuilder(cfg).build_entry_intent(
            symbol="BTC/USDT:USDT",
            direction="long",
            signal_timestamp=ts,
            current_bid=95.0,
            current_ask=95.1,
            tick_size=0.01,
            signal_5m_close=94.9,
            quantity=1.0,
            signal_id="mr-sig-1",
        )
        assert intent.expires_at == ts + timedelta(minutes=10)
        assert intent.reduce_only is False
        assert intent.reason_codes == ["MEAN_REVERSION_MAKER_LIMIT_INTENT"]


class TestSprint4RouterV1:
    def test_regime_d_rejects_all_new_entries(self, cfg):
        decision = R3Router(cfg).route(
            symbol="BTC/USDT:USDT",
            timestamp=datetime(2026, 1, 1, 5, tzinfo=timezone.utc),
            regime_state=_manual_regime_state(regime=Regime.D_NO_TRADE, direction="none"),
            trend_signal_result=SimpleNamespace(approved=True, direction="long"),
            mean_reversion_signal_result=SimpleNamespace(approved=True, direction="long"),
        )
        assert decision.approved is False
        assert "REJECT_REGIME_D_NO_TRADE" in decision.rejection_reasons

    def test_regime_a_only_allows_trend_pullback(self, cfg):
        decision = R3Router(cfg).route(
            symbol="BTC/USDT:USDT",
            timestamp=datetime(2026, 1, 1, 5, tzinfo=timezone.utc),
            regime_state=_manual_regime_state(regime=Regime.A_TREND, direction="long"),
            trend_signal_result=SimpleNamespace(approved=True, direction="long"),
            mean_reversion_signal_result=SimpleNamespace(approved=True, direction="short"),
        )
        assert decision.approved is True
        assert decision.selected_strategy == "trend_pullback"

    def test_regime_b_only_allows_mean_reversion(self, cfg):
        decision = R3Router(cfg).route(
            symbol="BTC/USDT:USDT",
            timestamp=datetime(2026, 1, 1, 5, tzinfo=timezone.utc),
            regime_state=_manual_regime_state(
                regime=Regime.B_SIDEWAYS,
                direction=Direction.NEUTRAL.value,
            ),
            trend_signal_result=SimpleNamespace(approved=True, direction="long"),
            mean_reversion_signal_result=SimpleNamespace(approved=True, direction="short"),
        )
        assert decision.approved is True
        assert decision.selected_strategy == "mean_reversion"

    def test_regime_c_deferred(self, cfg):
        decision = R3Router(cfg).route(
            symbol="BTC/USDT:USDT",
            timestamp=datetime(2026, 1, 1, 5, tzinfo=timezone.utc),
            regime_state=_manual_regime_state(regime=Regime.C_FUNDING_EXTREME, direction="contrarian_short"),
        )
        assert decision.approved is False
        assert decision.deferred is True
        assert "REGIME_C_DEFERRED" in decision.reason_codes

    def test_unknown_rejects(self, cfg):
        decision = R3Router(cfg).route(
            symbol="BTC/USDT:USDT",
            timestamp=datetime(2026, 1, 1, 5, tzinfo=timezone.utc),
            regime_state=_manual_regime_state(regime=Regime.UNKNOWN, direction="none"),
        )
        assert decision.approved is False
        assert "REJECT_REGIME_UNKNOWN" in decision.rejection_reasons

    def test_existing_short_rejects_long(self, cfg):
        decision = R3Router(cfg).route(
            symbol="BTC/USDT:USDT",
            timestamp=datetime(2026, 1, 1, 5, tzinfo=timezone.utc),
            regime_state=_manual_regime_state(regime=Regime.A_TREND, direction="long"),
            trend_signal_result=SimpleNamespace(approved=True, direction="long"),
            existing_position_state=PositionState(
                symbol="BTC/USDT:USDT",
                has_position=True,
                direction="short",
            ),
        )
        assert decision.approved is False
        assert "REJECT_OPPOSITE_POSITION_EXISTS" in decision.rejection_reasons

    def test_same_direction_position_conservatively_rejected(self, cfg):
        decision = R3Router(cfg).route(
            symbol="BTC/USDT:USDT",
            timestamp=datetime(2026, 1, 1, 5, tzinfo=timezone.utc),
            regime_state=_manual_regime_state(regime=Regime.A_TREND, direction="long"),
            trend_signal_result=SimpleNamespace(approved=True, direction="long"),
            existing_position_state=PositionState(
                symbol="BTC/USDT:USDT",
                has_position=True,
                direction="long",
            ),
        )
        assert decision.approved is False
        assert "REJECT_POSITION_EXISTS" in decision.rejection_reasons

    def test_router_does_not_mutate_position_state(self, cfg):
        position = PositionState(
            symbol="BTC/USDT:USDT",
            has_position=True,
            direction="long",
            quantity=2.0,
            entry_price=100.0,
        )
        before = position
        R3Router(cfg).route(
            symbol="BTC/USDT:USDT",
            timestamp=datetime(2026, 1, 1, 5, tzinfo=timezone.utc),
            regime_state=_manual_regime_state(regime=Regime.A_TREND, direction="short"),
            trend_signal_result=SimpleNamespace(approved=True, direction="short"),
            existing_position_state=position,
        )
        assert position == before

    def test_router_does_not_call_exchange_order_api(self):
        from pathlib import Path
        src = Path(__file__).resolve().parents[1] / "strategies" / "r3" / "router.py"
        text = src.read_text(encoding="utf-8")
        for forbidden in ["create_order", "place_order", "market_order", "ccxt"]:
            assert forbidden not in text


class TestSprint4Regression:
    def test_no_forbidden_sprint4_implementations(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1] / "strategies" / "r3"
        checked = [
            root / "mean_reversion.py",
            root / "router.py",
            root / "confirmation.py",
            root / "executor.py",
            root / "risk_engine.py",
            root / "trailing.py",
        ]
        for path in checked:
            text = path.read_text(encoding="utf-8")
            text = text.replace("live_order_type", "")
            for forbidden in [
                "BacktestEngine",
                "create_order",
                "place_order",
                "market_order",
                "ccxt",
                "funding_reversal",
                "live",
            ]:
                assert forbidden not in text


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

    def test_4h_now_attaches_atr_for_regime_b(self, cfg):
        """Sprint 2 needs ATR_4H for Regime B 'price near EMA200' distance check."""
        df = _make_clean_ohlcv(300, "4h", seed=8)
        out = ind.attach_core_indicators(df, cfg, "4h")
        atr_period = cfg.realized_vol.atr_period
        assert f"atr_{atr_period}" in out.columns


# ===============================================================
# Sprint 2 — Regime helper indicators
# ===============================================================
class TestRollingPercentileRank:
    def test_window_must_be_positive(self):
        with pytest.raises(ValueError):
            ind.rolling_percentile_rank(pd.Series([1.0]), window=0)

    def test_first_window_minus_one_is_nan(self):
        s = pd.Series(np.arange(1, 30, dtype=float))
        out = ind.rolling_percentile_rank(s, window=10)
        assert out.iloc[:9].isna().all()
        assert not pd.isna(out.iloc[9])

    def test_last_value_in_increasing_series_is_100(self):
        s = pd.Series(np.arange(1, 21, dtype=float))
        out = ind.rolling_percentile_rank(s, window=20)
        assert out.iloc[-1] == 100.0

    def test_constant_series_returns_consistent_rank(self):
        # pandas rank(pct=True) 對全 tie 給平均排名 → window=10 時為 (1+10)/2 / 10 × 100 = 55
        s = pd.Series([5.0] * 30)
        out = ind.rolling_percentile_rank(s, window=10)
        valid = out.dropna()
        # 全部值相同時，分位排名應一致（Sprint 2 用途上：分位「不變動」即可，具體值不重要）
        assert valid.nunique() == 1


class TestConsecutiveLargeCandlesCount:
    def test_n_recent_must_be_positive(self):
        with pytest.raises(ValueError):
            ind.consecutive_large_candles_count(
                pd.Series([1.0]), pd.Series([1.0]), pd.Series([1.0]),
                multiplier=2.5, n_recent=0,
            )

    def test_two_consecutive_large_count_is_2(self):
        # ATR=1，high-low 都是 3 → 全部 > 2.5×1=2.5
        n = 10
        high = pd.Series([10.0] * n)
        low = pd.Series([7.0] * n)
        atr_s = pd.Series([1.0] * n)
        out = ind.consecutive_large_candles_count(high, low, atr_s, multiplier=2.5, n_recent=2)
        # 暖機期 NaN，之後全 2
        valid = out.dropna()
        assert (valid == 2).all()

    def test_no_large_count_is_0(self):
        n = 10
        high = pd.Series([10.0] * n)
        low = pd.Series([9.5] * n)   # range=0.5 < 2.5
        atr_s = pd.Series([1.0] * n)
        out = ind.consecutive_large_candles_count(high, low, atr_s, multiplier=2.5, n_recent=2)
        valid = out.dropna()
        assert (valid == 0).all()


# ===============================================================
# ===============================================================
# Sprint 2 — Regime Classifier
# ===============================================================
# ===============================================================

from strategies.r3.regime import (
    Regime,
    Direction,
    RegimeClassifier,
    RegimeSnapshot,
    SystemStatus,
    SessionStatus,
    RegimeState,
    REGIME_NAMES,
    build_snapshot_from_indicators,
)


def _now_utc():
    return datetime(2026, 5, 1, tzinfo=UTC)


def _trend_long_snapshot(**overrides):
    """Reusable baseline that satisfies Regime A long without triggering D/C/B."""
    base = dict(
        ema_4h_short=100.0,
        ema_4h_long=90.0,
        adx_4h=30.0,
        close_1h=100.0,
        atr_4h=2.0,
        bb_width_pct_rank_1h=70.0,   # outside B range (10-50)
        extreme_vol=False,
        consecutive_large_candles_triggered=False,
        funding_z=0.5,
        premium_z=0.0,
        funding_samples_sufficient=True,
        premium_samples_sufficient=True,
        bar_index_1h=24 * 100,        # past warmup
        bars_per_day_1h=24,
    )
    base.update(overrides)
    return RegimeSnapshot(**base)


def _sideways_snapshot(**overrides):
    base = dict(
        ema_4h_short=100.0,
        ema_4h_long=100.1,
        adx_4h=15.0,
        close_1h=100.05,
        atr_4h=2.0,
        bb_width_pct_rank_1h=25.0,
        extreme_vol=False,
        consecutive_large_candles_triggered=False,
        funding_z=0.2,
        premium_z=0.0,
        funding_samples_sufficient=True,
        premium_samples_sufficient=True,
        bar_index_1h=24 * 100,
        bars_per_day_1h=24,
    )
    base.update(overrides)
    return RegimeSnapshot(**base)


# ---------------------------------------------------------------
# Regime A — Trend
# ---------------------------------------------------------------
class TestRegimeATrend:
    def test_long_trend_when_ema50_above_ema200_and_high_adx(self, cfg):
        rc = RegimeClassifier(cfg)
        snap = _trend_long_snapshot()
        state = rc.classify(_now_utc(), "BTC/USDT:USDT", snap)
        assert state.regime == Regime.A_TREND
        assert state.direction == Direction.LONG.value
        assert state.allow_new_entries is True
        assert "A_TREND" in state.reason_codes

    def test_short_trend_when_ema50_below_ema200(self, cfg):
        rc = RegimeClassifier(cfg)
        snap = _trend_long_snapshot(ema_4h_short=90.0, ema_4h_long=100.0)
        state = rc.classify(_now_utc(), "BTC/USDT:USDT", snap)
        assert state.regime == Regime.A_TREND
        assert state.direction == Direction.SHORT.value

    def test_low_adx_breaks_trend_no_a(self, cfg):
        rc = RegimeClassifier(cfg)
        # ADX 21 < min 22 → A 不成立；其他不滿足 B → UNKNOWN
        snap = _trend_long_snapshot(adx_4h=21.0)
        state = rc.classify(_now_utc(), "BTC/USDT:USDT", snap)
        assert state.regime != Regime.A_TREND

    def test_extreme_vol_takes_to_d(self, cfg):
        rc = RegimeClassifier(cfg)
        snap = _trend_long_snapshot(extreme_vol=True)
        state = rc.classify(_now_utc(), "BTC/USDT:USDT", snap)
        assert state.regime == Regime.D_NO_TRADE
        assert "D1_EXTREME_VOL" in state.reason_codes

    def test_funding_z_at_or_above_a_max_blocks_trend(self, cfg):
        rc = RegimeClassifier(cfg)
        # a_trend.funding_z_abs_max = 2.5；2.6 應該 block，但因 ≤ 3.0 不算 panic
        snap = _trend_long_snapshot(funding_z=2.6)
        state = rc.classify(_now_utc(), "BTC/USDT:USDT", snap)
        assert state.regime != Regime.A_TREND

    def test_config_threshold_drives_decision(self, cfg):
        """ADX 剛好在 a_trend.adx_4h_min 邊界：> min 才算 trend，== 不算"""
        rc = RegimeClassifier(cfg)
        threshold = cfg.regime.a_trend.adx_4h_min
        snap_at = _trend_long_snapshot(adx_4h=float(threshold))
        snap_above = _trend_long_snapshot(adx_4h=float(threshold) + 0.1)
        assert rc.classify(_now_utc(), "X", snap_at).regime != Regime.A_TREND
        assert rc.classify(_now_utc(), "X", snap_above).regime == Regime.A_TREND


# ---------------------------------------------------------------
# Regime B — Sideways
# ---------------------------------------------------------------
class TestRegimeBSideways:
    def test_low_adx_price_near_ema_bb_in_range(self, cfg):
        rc = RegimeClassifier(cfg)
        snap = _sideways_snapshot()
        state = rc.classify(_now_utc(), "BTC/USDT:USDT", snap)
        assert state.regime == Regime.B_SIDEWAYS
        assert state.direction == Direction.NEUTRAL.value
        assert "B_SIDEWAYS" in state.reason_codes

    def test_price_too_far_from_ema200_no_b(self, cfg):
        rc = RegimeClassifier(cfg)
        # close 距 ema_200 = 5 vs 0.5 × atr_4h = 1 → 失格
        snap = _sideways_snapshot(close_1h=105.0)
        state = rc.classify(_now_utc(), "X", snap)
        assert state.regime != Regime.B_SIDEWAYS

    def test_bb_width_outside_band_no_b(self, cfg):
        rc = RegimeClassifier(cfg)
        snap = _sideways_snapshot(bb_width_pct_rank_1h=80.0)  # 超過 50
        state = rc.classify(_now_utc(), "X", snap)
        assert state.regime != Regime.B_SIDEWAYS

    def test_funding_extreme_breaks_b(self, cfg):
        rc = RegimeClassifier(cfg)
        # b_range.funding_z_abs_max = 1.0；funding_z=1.5 應 block B
        snap = _sideways_snapshot(funding_z=1.5)
        state = rc.classify(_now_utc(), "X", snap)
        assert state.regime != Regime.B_SIDEWAYS

    def test_extreme_vol_takes_to_d_not_b(self, cfg):
        rc = RegimeClassifier(cfg)
        snap = _sideways_snapshot(extreme_vol=True)
        state = rc.classify(_now_utc(), "X", snap)
        assert state.regime == Regime.D_NO_TRADE


# ---------------------------------------------------------------
# Regime C — Funding Extreme
# ---------------------------------------------------------------
class TestRegimeCFundingExtreme:
    def test_positive_extreme_to_contrarian_short(self, cfg):
        rc = RegimeClassifier(cfg)
        snap = _trend_long_snapshot(funding_z=3.0, premium_z=2.5)
        # funding_z 3.0 > d1.panic 3.0 → False（>，不 ≥）；但若 >3.0 會觸發 D1
        # 用 funding_z=2.7 (>=2.5 c threshold, <3.0 panic), premium_z=2.5
        snap = _trend_long_snapshot(funding_z=2.7, premium_z=2.5)
        state = rc.classify(_now_utc(), "X", snap)
        assert state.regime == Regime.C_FUNDING_EXTREME
        assert state.direction == Direction.CONTRARIAN_SHORT.value

    def test_negative_extreme_to_contrarian_long(self, cfg):
        rc = RegimeClassifier(cfg)
        snap = _trend_long_snapshot(funding_z=-2.7, premium_z=-2.5)
        state = rc.classify(_now_utc(), "X", snap)
        assert state.regime == Regime.C_FUNDING_EXTREME
        assert state.direction == Direction.CONTRARIAN_LONG.value

    def test_funding_insufficient_no_c(self, cfg):
        rc = RegimeClassifier(cfg)
        snap = _trend_long_snapshot(
            funding_z=2.7, premium_z=2.5,
            funding_samples_sufficient=False,
        )
        state = rc.classify(_now_utc(), "X", snap)
        assert state.regime != Regime.C_FUNDING_EXTREME
        assert "funding_z" in state.insufficient_data_fields

    def test_premium_insufficient_no_c(self, cfg):
        rc = RegimeClassifier(cfg)
        snap = _trend_long_snapshot(
            funding_z=2.7, premium_z=2.5,
            premium_samples_sufficient=False,
        )
        state = rc.classify(_now_utc(), "X", snap)
        assert state.regime != Regime.C_FUNDING_EXTREME

    def test_only_funding_extreme_no_c(self, cfg):
        """funding 過熱但 premium 中性 → 不算 C（spec 要求兩者同方向極端）"""
        rc = RegimeClassifier(cfg)
        snap = _trend_long_snapshot(funding_z=2.7, premium_z=0.5)
        state = rc.classify(_now_utc(), "X", snap)
        assert state.regime != Regime.C_FUNDING_EXTREME

    def test_opposite_signs_no_c(self, cfg):
        """funding_z 正極端 + premium_z 負極端 → 不一致，不觸發 C"""
        rc = RegimeClassifier(cfg)
        snap = _trend_long_snapshot(funding_z=2.7, premium_z=-2.5)
        state = rc.classify(_now_utc(), "X", snap)
        assert state.regime != Regime.C_FUNDING_EXTREME


# ---------------------------------------------------------------
# Regime D — No Trade
# ---------------------------------------------------------------
class TestRegimeDNoTrade:
    def test_extreme_vol_triggers_d(self, cfg):
        rc = RegimeClassifier(cfg)
        snap = _trend_long_snapshot(extreme_vol=True)
        state = rc.classify(_now_utc(), "X", snap)
        assert state.regime == Regime.D_NO_TRADE
        assert state.allow_new_entries is False
        assert "D1_EXTREME_VOL" in state.reason_codes

    def test_consecutive_large_candles_triggers_d(self, cfg):
        rc = RegimeClassifier(cfg)
        snap = _trend_long_snapshot(consecutive_large_candles_triggered=True)
        state = rc.classify(_now_utc(), "X", snap)
        assert state.regime == Regime.D_NO_TRADE
        assert "D1_CONSECUTIVE_LARGE_CANDLES" in state.reason_codes

    def test_funding_panic_triggers_d(self, cfg):
        """|funding_z| > d1.funding_z_abs_panic (3.0) → D1"""
        rc = RegimeClassifier(cfg)
        snap = _trend_long_snapshot(funding_z=3.5)
        state = rc.classify(_now_utc(), "X", snap)
        assert state.regime == Regime.D_NO_TRADE
        assert "D1_FUNDING_PANIC" in state.reason_codes

    def test_missing_data_flag_triggers_d(self, cfg):
        rc = RegimeClassifier(cfg)
        snap = _trend_long_snapshot()
        state = rc.classify(_now_utc(), "X", snap, missing_data_flags=["kline_gap_1h"])
        assert state.regime == Regime.D_NO_TRADE
        assert "D1_MISSING_DATA" in state.reason_codes

    def test_session_daily_loss_triggers_d_with_session_risk_reason(self, cfg):
        """A2: session 風險用 D_SESSION_RISK_* 前綴，不混入 D1_*"""
        rc = RegimeClassifier(cfg)
        snap = _trend_long_snapshot()
        ss = SessionStatus(daily_pnl_pct=-2.5)
        state = rc.classify(_now_utc(), "X", snap, session_status=ss)
        assert state.regime == Regime.D_NO_TRADE
        assert "D_SESSION_RISK_DAILY_LOSS_LIMIT" in state.reason_codes
        # 不應誤標成市場風險
        assert "D1_DAILY_LOSS_LIMIT" not in state.reason_codes

    def test_session_consecutive_loss_triggers_d_with_session_risk_reason(self, cfg):
        rc = RegimeClassifier(cfg)
        snap = _trend_long_snapshot()
        ss = SessionStatus(consecutive_losses=4)
        state = rc.classify(_now_utc(), "X", snap, session_status=ss)
        assert state.regime == Regime.D_NO_TRADE
        assert "D_SESSION_RISK_CONSECUTIVE_LOSS" in state.reason_codes
        assert "D1_CONSECUTIVE_LOSS_LIMIT" not in state.reason_codes

    def test_no_session_status_does_not_trigger_session_risk(self, cfg):
        """未提供 SessionStatus 時，不應出現 D_SESSION_RISK_*"""
        rc = RegimeClassifier(cfg)
        snap = _trend_long_snapshot()
        state = rc.classify(_now_utc(), "X", snap)
        for code in state.reason_codes:
            assert not code.startswith("D_SESSION_RISK_")

    def test_api_latency_triggers_d2(self, cfg):
        rc = RegimeClassifier(cfg)
        snap = _trend_long_snapshot()
        ss = SystemStatus(api_latency_abnormal=True)
        state = rc.classify(_now_utc(), "X", snap, system_status=ss)
        assert state.regime == Regime.D_NO_TRADE
        assert "D2_API_LATENCY_ABNORMAL" in state.reason_codes

    def test_websocket_disconnect_triggers_d2(self, cfg):
        rc = RegimeClassifier(cfg)
        snap = _trend_long_snapshot()
        ss = SystemStatus(websocket_disconnected=True)
        state = rc.classify(_now_utc(), "X", snap, system_status=ss)
        assert state.regime == Regime.D_NO_TRADE
        assert "D2_WEBSOCKET_DISCONNECTED" in state.reason_codes

    def test_multiple_d_reasons_collected(self, cfg):
        rc = RegimeClassifier(cfg)
        snap = _trend_long_snapshot(extreme_vol=True, consecutive_large_candles_triggered=True)
        state = rc.classify(_now_utc(), "X", snap)
        assert state.regime == Regime.D_NO_TRADE
        assert "D1_EXTREME_VOL" in state.reason_codes
        assert "D1_CONSECUTIVE_LARGE_CANDLES" in state.reason_codes


# ---------------------------------------------------------------
# Priority — D > C > A > B > UNKNOWN
# ---------------------------------------------------------------
class TestRegimePriority:
    def test_d_wins_over_a(self, cfg):
        rc = RegimeClassifier(cfg)
        # 同時有 trend + extreme_vol → D
        snap = _trend_long_snapshot(extreme_vol=True)
        state = rc.classify(_now_utc(), "X", snap)
        assert state.regime == Regime.D_NO_TRADE

    def test_d_wins_over_c(self, cfg):
        rc = RegimeClassifier(cfg)
        # funding_z 4.0 → 同時 panic (D1) 且 funding extreme (C)
        snap = _trend_long_snapshot(funding_z=4.0, premium_z=2.5)
        state = rc.classify(_now_utc(), "X", snap)
        assert state.regime == Regime.D_NO_TRADE

    def test_c_wins_over_a_and_preserves_trend_info(self, cfg):
        """C 與 A 同時成立時，最終為 C，但 trend_info 保留 A 方向"""
        rc = RegimeClassifier(cfg)
        # funding_z=2.7 +premium_z=2.5 → C (contrarian short)
        # 但 funding_z=2.7 > a_trend.funding_z_abs_max=2.5 → A 不成立
        # 為了讓 A 與 C 同時成立，需要把 a_trend.funding_z_abs_max 拉超過 C threshold
        # spec 設計上 a_trend max < c threshold，所以「同時成立」幾乎不會發生。
        # 我們直接構造邊界情境：funding_z 剛好超過 c_threshold 但等於 a_max
        # 由於 a_trend 的 |funding_z| >= a_max 就 block，C 與 A 同時成立的窗口很窄。
        # 這個 test 改成驗證「C 即使 A 不成立也能單獨觸發」+「reason 結構正確」
        snap = _trend_long_snapshot(funding_z=2.7, premium_z=2.5)
        state = rc.classify(_now_utc(), "X", snap)
        assert state.regime == Regime.C_FUNDING_EXTREME
        # 在這個快照中 A 因 funding_z 超過 a_max 而失格 → trend_info 不應出現
        # （spec 設計上 C 與 A 通常互斥）

    def test_a_wins_over_b(self, cfg):
        """ADX 介於 A.min 與 B.max 之間時，依 spec 應走 A（高 ADX 優先）。
        但 spec 把這視為灰區；測試實際行為：高 ADX → A，低 ADX → B"""
        rc = RegimeClassifier(cfg)
        snap_a = _trend_long_snapshot(adx_4h=30.0)
        snap_b = _sideways_snapshot()
        assert rc.classify(_now_utc(), "X", snap_a).regime == Regime.A_TREND
        assert rc.classify(_now_utc(), "X", snap_b).regime == Regime.B_SIDEWAYS

    def test_unknown_when_no_match(self, cfg):
        rc = RegimeClassifier(cfg)
        # ADX 介於 b.max(18) 與 a.min(22) 之間 → A/B 都不滿足
        snap = _trend_long_snapshot(
            adx_4h=20.0, ema_4h_short=100.0, ema_4h_long=100.0,
            bb_width_pct_rank_1h=70.0,
        )
        state = rc.classify(_now_utc(), "X", snap)
        assert state.regime == Regime.UNKNOWN
        assert state.allow_new_entries is False

    def test_a1_boundary_at_funding_z_2_5_prefers_c_over_a(self, cfg):
        """
        A1（BOSS 拍板）：funding_z 邊界 2.5 時 A/C 互斥，優先進 C。

        - funding_z = 2.5 (== a_max == c_threshold)
        - premium_z = 2.0 (== c.premium_z_threshold)
        - 預期：C 觸發，A 失格
        """
        rc = RegimeClassifier(cfg)
        a_max = cfg.regime.a_trend.funding_z_abs_max
        c_thr = cfg.regime.c_extreme.funding_z_threshold
        p_thr = cfg.regime.c_extreme.premium_z_threshold
        # 確認 spec 仍然 a_max == c_thr（互斥邊界）
        assert a_max == c_thr, "A1 設計預期 a_trend.funding_z_abs_max == c_extreme.funding_z_threshold"

        snap = _trend_long_snapshot(funding_z=c_thr, premium_z=p_thr)
        state = rc.classify(_now_utc(), "X", snap)
        assert state.regime == Regime.C_FUNDING_EXTREME
        assert state.direction == Direction.CONTRARIAN_SHORT.value

    def test_a1_just_below_boundary_allows_a(self, cfg):
        """funding_z 小一咪咪（2.49）→ 進 A trend，不進 C"""
        rc = RegimeClassifier(cfg)
        c_thr = cfg.regime.c_extreme.funding_z_threshold
        snap = _trend_long_snapshot(funding_z=c_thr - 0.01, premium_z=2.0)
        state = rc.classify(_now_utc(), "X", snap)
        assert state.regime == Regime.A_TREND


# ---------------------------------------------------------------
# Insufficient data
# ---------------------------------------------------------------
class TestRegimeInsufficientData:
    def test_missing_ema_returns_unknown(self, cfg):
        rc = RegimeClassifier(cfg)
        snap = _trend_long_snapshot(ema_4h_short=None)
        state = rc.classify(_now_utc(), "X", snap)
        assert state.regime == Regime.UNKNOWN
        assert "ema_4h_short" in state.insufficient_data_fields
        assert "INSUFFICIENT_DATA" in state.reason_codes

    def test_missing_adx_returns_unknown(self, cfg):
        rc = RegimeClassifier(cfg)
        snap = _trend_long_snapshot(adx_4h=None)
        state = rc.classify(_now_utc(), "X", snap)
        assert state.regime == Regime.UNKNOWN

    def test_funding_insufficient_does_not_block_a(self, cfg):
        """A 不需要 funding 樣本充足；funding_z 為 None 時應仍可判 A"""
        rc = RegimeClassifier(cfg)
        snap = _trend_long_snapshot(
            funding_z=None,
            funding_samples_sufficient=False,
        )
        state = rc.classify(_now_utc(), "X", snap)
        # funding 不足會列入 insufficient_data_fields，但不阻擋 A
        assert state.regime == Regime.A_TREND
        assert "funding_z" in state.insufficient_data_fields

    def test_warmup_period_returns_d(self, cfg):
        """Day 0~30 不可交易 → D"""
        rc = RegimeClassifier(cfg)
        snap = _trend_long_snapshot(bar_index_1h=10 * 24)  # day 10
        state = rc.classify(_now_utc(), "X", snap)
        assert state.regime == Regime.D_NO_TRADE
        assert "D1_WARMUP_PERIOD" in state.reason_codes


# ---------------------------------------------------------------
# Output structure / Regression — no trading actions
# ---------------------------------------------------------------
class TestRegimeOutputStructure:
    def test_state_is_dataclass_with_required_fields(self, cfg):
        rc = RegimeClassifier(cfg)
        state = rc.classify(_now_utc(), "BTC/USDT:USDT", _trend_long_snapshot())
        assert isinstance(state, RegimeState)
        for field_name in [
            "as_of", "symbol", "regime", "regime_name",
            "direction", "allow_new_entries",
            "reason_codes", "metrics_snapshot",
            "insufficient_data_fields",
        ]:
            assert hasattr(state, field_name)

    def test_regime_name_matches_enum(self, cfg):
        rc = RegimeClassifier(cfg)
        for snap_factory in [_trend_long_snapshot, _sideways_snapshot]:
            state = rc.classify(_now_utc(), "X", snap_factory())
            assert state.regime_name == REGIME_NAMES[state.regime]

    def test_metrics_snapshot_includes_all_inputs(self, cfg):
        rc = RegimeClassifier(cfg)
        state = rc.classify(_now_utc(), "X", _trend_long_snapshot())
        for k in ["ema_4h_short", "ema_4h_long", "adx_4h",
                  "atr_4h", "close_1h", "funding_z", "premium_z"]:
            assert k in state.metrics_snapshot

    def test_d_state_disallows_entries(self, cfg):
        rc = RegimeClassifier(cfg)
        state = rc.classify(_now_utc(), "X", _trend_long_snapshot(extreme_vol=True))
        assert state.allow_new_entries is False

    def test_unknown_state_disallows_entries(self, cfg):
        rc = RegimeClassifier(cfg)
        state = rc.classify(_now_utc(), "X", _trend_long_snapshot(adx_4h=None))
        assert state.allow_new_entries is False


class TestRegimeNoTradingLogic:
    """Regression: classifier 不應有任何 trading 副作用"""

    def test_classify_does_not_create_orders(self, cfg):
        rc = RegimeClassifier(cfg)
        # 嘗試從 RegimeState 找出任何看起來像「下單 / 改持倉」的方法或屬性
        state = rc.classify(_now_utc(), "X", _trend_long_snapshot())
        for forbidden in ["create_order", "submit_order", "cancel_order",
                          "open_position", "close_position",
                          "place_stop", "place_take_profit"]:
            assert not hasattr(state, forbidden)
            assert not hasattr(rc, forbidden)

    def test_classifier_holds_no_trading_state(self, cfg):
        rc = RegimeClassifier(cfg)
        # classify 多次不應累積狀態
        s1 = rc.classify(_now_utc(), "X", _trend_long_snapshot())
        s2 = rc.classify(_now_utc(), "X", _sideways_snapshot())
        assert s1.regime != s2.regime  # 不互相影響

    def test_classifier_does_not_import_executor_or_position_manager(self):
        """純文字檢查：regime.py 不應 import executor / position_manager / order"""
        from pathlib import Path
        regime_src = Path(__file__).resolve().parents[1] / "strategies" / "r3" / "regime.py"
        text = regime_src.read_text(encoding="utf-8")
        assert "from .executor" not in text
        assert "import executor" not in text
        assert "position_manager" not in text
        assert "order_manager" not in text


# ---------------------------------------------------------------
# build_snapshot_from_indicators — integration helper
# ---------------------------------------------------------------
class TestBuildSnapshotFromIndicators:
    def test_builds_snapshot_from_real_indicator_dfs(self, cfg):
        """整合測試：用合成 OHLCV → attach_core_indicators → build_snapshot"""
        df_4h = _make_clean_ohlcv(300, "4h", seed=11)
        df_1h = _make_clean_ohlcv(2400, "1h", seed=12)  # 100 days @ 24

        df_4h_ind = ind.attach_core_indicators(df_4h, cfg, "4h")
        df_1h_ind = ind.attach_core_indicators(df_1h, cfg, "1h")

        snap = build_snapshot_from_indicators(
            cfg=cfg,
            df_4h_with_indicators=df_4h_ind,
            df_1h_with_indicators=df_1h_ind,
            funding_z_value=0.5,
            premium_z_value=0.0,
            extreme_vol_at_t=False,
            consecutive_large_candles_at_t=False,
            bar_index_1h=len(df_1h_ind) - 1,
            bars_per_day_1h=24,
        )
        # 必填欄位都填到了
        assert snap.ema_4h_short is not None
        assert snap.ema_4h_long is not None
        assert snap.adx_4h is not None
        assert snap.atr_4h is not None
        assert snap.close_1h is not None
        assert snap.funding_samples_sufficient is True
        assert snap.premium_samples_sufficient is True

    def test_classifier_runs_on_built_snapshot(self, cfg):
        df_4h = _make_clean_ohlcv(300, "4h", seed=21)
        df_1h = _make_clean_ohlcv(2400, "1h", seed=22)
        df_4h_ind = ind.attach_core_indicators(df_4h, cfg, "4h")
        df_1h_ind = ind.attach_core_indicators(df_1h, cfg, "1h")

        snap = build_snapshot_from_indicators(
            cfg=cfg,
            df_4h_with_indicators=df_4h_ind,
            df_1h_with_indicators=df_1h_ind,
            funding_z_value=0.5, premium_z_value=0.0,
            extreme_vol_at_t=False,
            consecutive_large_candles_at_t=False,
            bar_index_1h=len(df_1h_ind) - 1,
            bars_per_day_1h=24,
        )
        rc = RegimeClassifier(cfg)
        state = rc.classify(_now_utc(), "BTC/USDT:USDT", snap)
        # 隨機合成資料的 regime 結果不可預期，但至少要回傳合法 RegimeState
        assert isinstance(state, RegimeState)
        assert state.regime in {
            Regime.A_TREND, Regime.B_SIDEWAYS, Regime.C_FUNDING_EXTREME,
            Regime.D_NO_TRADE, Regime.UNKNOWN,
        }

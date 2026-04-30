"""
R3 Strategy — Unit Tests for Q21~Q29
=====================================

Spec   : docs/R3_spec.md
Config : config/r3_strategy.yaml

工程紀律
--------
- 每個 Q 必須至少一個 test，覆蓋 default + 至少一個 edge case
- 測試 fail 時，禁止偷偷調 spec 讓它通過（必須改 code 或回報失敗）
- 所有參數從 R3Config 取，禁止 hardcode
"""
from __future__ import annotations

import pytest

from strategies.r3.config_loader import R3Config


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

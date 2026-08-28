"""Tests for the option_sell_guard rule."""

from firewall.rules.base import RuleConfig
from firewall.rules.option_sell_guard import OptionSellGuardRule


def _rule(**params) -> OptionSellGuardRule:
    config = RuleConfig.model_validate(
        {
            "id": "test-option-sell-guard",
            "type": "option_sell_guard",
            "severity": "hard",
            "regulation_ref": None,
            **params,
        }
    )
    return OptionSellGuardRule(config)


def test_single_leg_buy_passes():
    rule = _rule()

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "buy", "qty": "1"},
        {},
    )

    assert not outcome.triggered


def test_single_leg_sell_hard_blocks():
    rule = _rule()

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "sell", "qty": "1"},
        {},
    )

    assert outcome.triggered
    assert "not yet supported" in outcome.reason
    assert "scope limitation" in outcome.reason


def test_default_side_is_buy():
    rule = _rule()

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "qty": "1"},  # no "side" field
        {},
    )

    assert not outcome.triggered


def test_multi_leg_all_buy_passes():
    rule = _rule()

    outcome = rule.check(
        "place_option_order",
        {
            "qty": "1",
            "order_class": "mleg",
            "legs": [
                {"symbol": "AAPL260918C00150000", "ratio_qty": "1", "side": "buy"},
                {"symbol": "AAPL260918C00160000", "ratio_qty": "1", "side": "buy"},
            ],
        },
        {},
    )

    assert not outcome.triggered


def test_multi_leg_with_one_sell_leg_hard_blocks():
    # This is exactly a collar (sell call, buy put) shape -- out of scope
    # per this rule's own module docstring.
    rule = _rule()

    outcome = rule.check(
        "place_option_order",
        {
            "qty": "1",
            "order_class": "mleg",
            "legs": [
                {"symbol": "AAPL260918C00150000", "ratio_qty": "1", "side": "buy"},
                {"symbol": "AAPL260918C00160000", "ratio_qty": "1", "side": "sell"},
            ],
        },
        {},
    )

    assert outcome.triggered
    assert "not a verified buy" in outcome.reason


def test_multi_leg_missing_leg_side_fails_closed():
    # A leg present but with no `side` at all is missing real data this
    # rule cannot positively clear as a buy -- must block, not default.
    rule = _rule()

    outcome = rule.check(
        "place_option_order",
        {
            "qty": "1",
            "order_class": "mleg",
            "legs": [{"symbol": "AAPL260918C00150000", "ratio_qty": "1"}],
        },
        {},
    )

    assert outcome.triggered
    assert "not a verified buy" in outcome.reason


def test_multi_leg_unparseable_leg_side_fails_closed():
    rule = _rule()

    outcome = rule.check(
        "place_option_order",
        {
            "qty": "1",
            "order_class": "mleg",
            "legs": [{"symbol": "AAPL260918C00150000", "ratio_qty": "1", "side": 123}],
        },
        {},
    )

    assert outcome.triggered
    assert "not a verified buy" in outcome.reason


def test_stock_sell_order_is_unchecked():
    # side == "sell" is completely normal for a stock order (selling shares
    # you hold) -- this rule must never see it at all.
    rule = _rule()

    outcome = rule.check(
        "place_stock_order",
        {"symbol": "AAPL", "side": "sell", "qty": "10"},
        {},
    )

    assert not outcome.triggered


def test_crypto_sell_order_is_unchecked():
    rule = _rule()

    outcome = rule.check(
        "place_crypto_order",
        {"symbol": "BTC/USD", "side": "sell", "qty": "1"},
        {},
    )

    assert not outcome.triggered

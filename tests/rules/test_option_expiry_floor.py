"""Tests for the option_expiry_floor rule."""

from datetime import datetime, timezone

from firewall.rules.base import RuleConfig
from firewall.rules.option_expiry_floor import OptionExpiryFloorRule

# Fixed "now" for deterministic DTE math: 2026-09-11 00:00:00 UTC.
_NOW = datetime(2026, 9, 11, tzinfo=timezone.utc).timestamp()


def _rule(**params) -> OptionExpiryFloorRule:
    config = RuleConfig.model_validate(
        {
            "id": "test-option-expiry-floor",
            "type": "option_expiry_floor",
            "severity": "hard",
            "regulation_ref": None,
            **params,
        }
    )
    return OptionExpiryFloorRule(config)


def test_non_order_tool_is_unchecked():
    rule = _rule(days_to_expiry_floor=7)

    outcome = rule.check("get_orders", {"symbol": "AAPL260918P00220000"}, {})

    assert not outcome.triggered


def test_single_leg_expiry_below_floor_hard_blocks():
    rule = _rule(days_to_expiry_floor=7)

    # 2026-09-18 is exactly 7 calendar days from 2026-09-11 -- use one day
    # closer to land inside the floor.
    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260917P00220000", "side": "buy", "qty": "1"},
        {"now": _NOW},
    )

    assert outcome.triggered
    assert "contract expiration within 7 days" in outcome.reason
    assert "pin risk" in outcome.reason
    assert "theta decay" in outcome.reason


def test_single_leg_expiry_exactly_at_floor_passes():
    # "below a configured floor" -- dte == floor must NOT trigger.
    rule = _rule(days_to_expiry_floor=7)

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "buy", "qty": "1"},
        {"now": _NOW},
    )

    assert not outcome.triggered


def test_single_leg_expiry_well_beyond_floor_passes():
    rule = _rule(days_to_expiry_floor=7)

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL261218P00220000", "side": "buy", "qty": "1"},
        {"now": _NOW},
    )

    assert not outcome.triggered


def test_already_expired_contract_hard_blocks():
    rule = _rule(days_to_expiry_floor=7)

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260901P00220000", "side": "buy", "qty": "1"},
        {"now": _NOW},
    )

    assert outcome.triggered


def test_multi_leg_any_leg_below_floor_hard_blocks():
    rule = _rule(days_to_expiry_floor=7)

    outcome = rule.check(
        "place_option_order",
        {
            "qty": "10",
            "order_class": "mleg",
            "legs": [
                {"symbol": "AAPL261218C00150000", "ratio_qty": "1", "side": "buy"},
                {"symbol": "AAPL260913C00160000", "ratio_qty": "1", "side": "sell"},
            ],
        },
        {"now": _NOW},
    )

    assert outcome.triggered


def test_multi_leg_all_legs_beyond_floor_passes():
    rule = _rule(days_to_expiry_floor=7)

    outcome = rule.check(
        "place_option_order",
        {
            "qty": "10",
            "order_class": "mleg",
            "legs": [
                {"symbol": "AAPL261218C00150000", "ratio_qty": "1", "side": "buy"},
                {"symbol": "AAPL261218C00160000", "ratio_qty": "1", "side": "sell"},
            ],
        },
        {"now": _NOW},
    )

    assert not outcome.triggered


def test_multi_leg_with_unparseable_leg_symbol_is_skipped_not_failed_closed():
    # This rule's job is DTE, not symbol-format validation -- an
    # unparseable leg symbol is symbol_allowlist's fail-closed
    # responsibility (see its own tests), not duplicated here.
    rule = _rule(days_to_expiry_floor=7)

    outcome = rule.check(
        "place_option_order",
        {
            "qty": "10",
            "order_class": "mleg",
            "legs": [{"symbol": "not-an-occ-symbol", "ratio_qty": "1", "side": "buy"}],
        },
        {"now": _NOW},
    )

    assert not outcome.triggered


def test_stock_order_with_no_option_symbol_is_unchecked():
    rule = _rule(days_to_expiry_floor=7)

    outcome = rule.check(
        "place_stock_order", {"symbol": "AAPL", "side": "buy", "qty": "10"}, {"now": _NOW}
    )

    assert not outcome.triggered


def test_default_floor_is_seven_days():
    rule = _rule()

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260913P00220000", "side": "buy", "qty": "1"},
        {"now": _NOW},
    )

    assert outcome.triggered


def test_missing_now_falls_back_to_real_clock():
    # Expiry pinned far enough out (year 2099) that "beyond the floor"
    # holds regardless of when this test actually runs -- the point is
    # only that omitting state["now"] doesn't raise, not a specific DTE.
    rule = _rule(days_to_expiry_floor=7)

    outcome = rule.check(
        "place_option_order", {"symbol": "AAPL991231P00220000", "side": "buy", "qty": "1"}, {}
    )

    assert not outcome.triggered

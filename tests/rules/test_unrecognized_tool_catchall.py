"""Tests for the unrecognized_tool_catchall rule."""

from firewall.rules.base import RuleConfig
from firewall.rules.unrecognized_tool_catchall import UnrecognizedToolCatchallRule


def _rule(**params) -> UnrecognizedToolCatchallRule:
    config = RuleConfig.model_validate(
        {
            "id": "test-unrecognized-tool-catchall",
            "type": "unrecognized_tool_catchall",
            "severity": "hard",
            "regulation_ref": None,
            "read_only_whitelist": ["get_clock", "get_account_info"],
            "covered_patterns": ["order", "cancel_all", "close_all", "liquidate"],
            **params,
        }
    )
    return UnrecognizedToolCatchallRule(config)


def test_unrecognized_tool_name_triggers():
    # Reproduces the conformance audit's A2 probe: a name on neither the
    # whitelist nor any covered pattern, carrying a huge dollar amount.
    rule = _rule()

    outcome = rule.check("zzz_never_seen_tool_xyz", {"amount_usd": 999999999.0}, {})

    assert outcome.triggered
    assert "zzz_never_seen_tool_xyz" in outcome.reason


def test_read_only_whitelisted_tool_does_not_trigger():
    rule = _rule()

    outcome = rule.check("get_clock", {}, {})

    assert not outcome.triggered


def test_whitelist_match_is_case_insensitive():
    rule = _rule()

    outcome = rule.check("GET_CLOCK", {}, {})

    assert not outcome.triggered


def test_tool_covered_by_another_rules_pattern_does_not_trigger():
    # place_stock_order isn't on the whitelist, but it matches the "order"
    # pattern already owned by notional_cap/position_cap/etc -- the
    # catchall must not double-block it.
    rule = _rule()

    outcome = rule.check("place_stock_order", {"symbol": "AAPL", "qty": 1}, {})

    assert not outcome.triggered


def test_tool_covered_by_bulk_action_pattern_does_not_trigger():
    rule = _rule()

    outcome = rule.check("close_all_positions", {}, {})

    assert not outcome.triggered


def test_real_but_currently_unmapped_action_tool_triggers():
    # close_position is a real Alpaca action tool that matches neither the
    # whitelist nor any covered pattern -- this is exactly the audit's A1
    # finding of an uncovered action tool, not a hypothetical.
    rule = _rule()

    outcome = rule.check("close_position", {"symbol_or_asset_id": "AAPL"}, {})

    assert outcome.triggered

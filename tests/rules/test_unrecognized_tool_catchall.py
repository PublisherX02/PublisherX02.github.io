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


def test_default_policy_and_presets_pass_catchall_coverage():
    import yaml
    from pathlib import Path
    from firewall.rules.unrecognized_tool_catchall import validate_catchall_coverage

    repo_root = Path(__file__).resolve().parents[2]
    policies_dir = repo_root / "policies"

    policy_files = [
        "default.yaml",
        "preset_1_loose.yaml",
        "preset_2.yaml",
        "preset_3.yaml",
        "preset_4.yaml",
        "preset_5_strict.yaml",
    ]

    for pfile in policy_files:
        data = yaml.safe_load((policies_dir / pfile).read_text(encoding="utf-8"))
        # Should execute cleanly with zero errors/drift
        validate_catchall_coverage(data["rules"])


def test_catchall_validation_fails_on_uncovered_rule_pattern():
    import pytest
    from firewall.rules.unrecognized_tool_catchall import validate_catchall_coverage

    rules = [
        {
            "id": "new-transfer-rule",
            "type": "notional_cap",
            "tool_match": ["transfer_funds"],
        },
        {
            "id": "unrecognized-tool-catchall",
            "type": "unrecognized_tool_catchall",
            "covered_patterns": ["order", "cancel_all"],
            "read_only_whitelist": ["get_clock"],
        },
    ]

    with pytest.raises(ValueError, match="Catchall pattern coverage drift detected") as exc_info:
        validate_catchall_coverage(rules)
    assert "transfer_funds" in str(exc_info.value)


def test_catchall_validation_fails_on_orphaned_covered_pattern():
    import pytest
    from firewall.rules.unrecognized_tool_catchall import validate_catchall_coverage

    rules = [
        {
            "id": "order-rule",
            "type": "notional_cap",
            "tool_match": ["order"],
        },
        {
            "id": "unrecognized-tool-catchall",
            "type": "unrecognized_tool_catchall",
            "covered_patterns": ["order", "nonexistent_action_xyz"],
            "read_only_whitelist": ["get_clock"],
        },
    ]

    with pytest.raises(ValueError, match="Orphaned covered_patterns") as exc_info:
        validate_catchall_coverage(rules)
    assert "nonexistent_action_xyz" in str(exc_info.value)


def test_catchall_validation_fails_on_action_tool_in_whitelist():
    import pytest
    from firewall.rules.unrecognized_tool_catchall import validate_catchall_coverage

    rules = [
        {
            "id": "order-rule",
            "type": "notional_cap",
            "tool_match": ["order"],
        },
        {
            "id": "unrecognized-tool-catchall",
            "type": "unrecognized_tool_catchall",
            "covered_patterns": ["order"],
            "read_only_whitelist": ["place_stock_order"],  # Action tool improperly whitelisted
        },
    ]

    with pytest.raises(ValueError, match="Action tools improperly listed on read_only_whitelist") as exc_info:
        validate_catchall_coverage(rules)
    assert "place_stock_order" in str(exc_info.value)


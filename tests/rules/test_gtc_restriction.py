"""Tests for the gtc_restriction rule."""

from firewall.rules.base import RuleConfig
from firewall.rules.gtc_restriction import GTCRestrictionRule


def _rule(**params) -> GTCRestrictionRule:
    config = RuleConfig.model_validate(
        {
            "id": "test-gtc-restriction",
            "type": "gtc_restriction",
            "severity": "hard",
            "regulation_ref": None,
            **params,
        }
    )
    return GTCRestrictionRule(config)


def test_gtc_order_rejected_by_default():
    rule = _rule()

    outcome = rule.check("place_order", {"time_in_force": "gtc"}, {})

    assert outcome.triggered
    assert outcome.reason == (
        "GTC orders are restricted by policy; resubmit as day or ioc."
    )


def test_gtc_order_allowed_when_policy_permits():
    rule = _rule(allow_gtc=True)

    outcome = rule.check("place_order", {"time_in_force": "gtc"}, {})

    assert not outcome.triggered


def test_day_order_passes():
    rule = _rule()

    outcome = rule.check("place_order", {"time_in_force": "day"}, {})

    assert not outcome.triggered


def test_ioc_order_passes():
    rule = _rule()

    outcome = rule.check("place_order", {"time_in_force": "ioc"}, {})

    assert not outcome.triggered


def test_gtc_check_is_case_insensitive():
    rule = _rule()

    outcome = rule.check("place_order", {"time_in_force": "GTC"}, {})

    assert outcome.triggered


def test_gtc_check_strips_whitespace():
    rule = _rule()

    outcome = rule.check("place_order", {"time_in_force": " gtc "}, {})

    assert outcome.triggered


def test_non_order_tool_ignored():
    rule = _rule(tool_match=["order"])

    outcome = rule.check("get_quote", {"time_in_force": "gtc"}, {})

    assert not outcome.triggered


def test_missing_time_in_force_field_passes():
    rule = _rule()

    outcome = rule.check("place_order", {}, {})

    assert not outcome.triggered


def test_check_does_not_mutate_arguments():
    rule = _rule()
    arguments = {"time_in_force": "gtc", "symbol": "AAPL"}
    original = dict(arguments)

    rule.check("place_order", arguments, {})

    assert arguments == original

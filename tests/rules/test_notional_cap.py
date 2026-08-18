"""Tests for the notional_cap rule."""

from firewall.rules.base import RuleConfig
from firewall.rules.notional_cap import NotionalCapRule


def _rule(**params) -> NotionalCapRule:
    config = RuleConfig.model_validate(
        {
            "id": "test-notional-cap",
            "type": "notional_cap",
            "severity": "hard",
            "regulation_ref": "SEC Rule 15c3-5(c)(1)(i)",
            **params,
        }
    )
    return NotionalCapRule(config)


def test_order_over_cap_triggers():
    rule = _rule(max_usd=1000)

    outcome = rule.check("place_order", {"qty": 10, "limit_price": 150}, {})

    assert outcome.triggered


def test_order_just_under_cap_passes():
    rule = _rule(max_usd=1500)

    # qty * price == 1499.90, just under the 1500 cap.
    outcome = rule.check("place_order", {"qty": 10, "limit_price": 149.99}, {})

    assert not outcome.triggered

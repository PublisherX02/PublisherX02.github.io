"""Tests for the blast_radius rule."""

from firewall.rules.base import RuleConfig
from firewall.rules.blast_radius import BlastRadiusRule


def _rule(**params) -> BlastRadiusRule:
    config = RuleConfig.model_validate(
        {
            "id": "test-blast-radius",
            "type": "blast_radius",
            "severity": "hard",
            "regulation_ref": "SEC Rule 15c3-5(b); FINRA Regulatory Notice 15-09",
            **params,
        }
    )
    return BlastRadiusRule(config)


def test_bulk_tool_without_approval_triggers():
    rule = _rule()

    outcome = rule.check("cancel_all_orders", {}, {})

    assert outcome.triggered


def test_bulk_tool_with_human_approval_passes():
    rule = _rule()

    outcome = rule.check("cancel_all_orders", {}, {"human_approved": True})

    assert not outcome.triggered

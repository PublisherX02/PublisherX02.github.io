"""Tests for the symbol_allowlist rule."""

from firewall.rules.base import RuleConfig
from firewall.rules.symbol_allowlist import SymbolAllowlistRule


def _rule(**params) -> SymbolAllowlistRule:
    config = RuleConfig.model_validate(
        {
            "id": "test-symbol-allowlist",
            "type": "symbol_allowlist",
            "severity": "hard",
            "regulation_ref": "SEC Rule 15c3-5(c)(2)(ii)",
            **params,
        }
    )
    return SymbolAllowlistRule(config)


def test_symbol_not_on_allowlist_triggers():
    rule = _rule(allowed_symbols=["AAPL", "MSFT"])

    outcome = rule.check("place_order", {"symbol": "TSLA"}, {})

    assert outcome.triggered


def test_symbol_on_allowlist_passes():
    rule = _rule(allowed_symbols=["AAPL", "MSFT"])

    outcome = rule.check("place_order", {"symbol": "AAPL"}, {})

    assert not outcome.triggered

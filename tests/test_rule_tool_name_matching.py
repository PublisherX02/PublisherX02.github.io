"""Regression test for the conformance-audit finding (A4): order_rate_throttle,
place_cancel_ratio, layering_detector, and wash_trade_detector all gate on
`place_tool_match`, which must actually match Alpaca's real order-placement
tool names (place_stock_order, place_option_order, place_crypto_order) --
not a fictional stand-in name ("place_order") that no test ever checked
against the real upstream tool list. This is the test that should have
caught that mismatch before the audit did.

Checks the actual configured pattern in every policies/*.yaml that declares
one of these four rule types -- never a synthetic/hand-picked pattern --
so a future edit that reintroduces a name matching no real tool fails here.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from firewall.rules._util import matches_any

REPO_ROOT = Path(__file__).parent.parent
POLICIES_DIR = REPO_ROOT / "policies"

# The real Alpaca MCP server's order-placement tools (see AUDIT.md section
# A1 -- enumerated directly from the real upstream server, not assumed).
REAL_ALPACA_ORDER_TOOLS = ("place_stock_order", "place_option_order", "place_crypto_order")

_RULE_TYPES_USING_PLACE_TOOL_MATCH = (
    "order_rate_throttle",
    "place_cancel_ratio",
    "layering_detector",
    "wash_trade_detector",
)


def _policy_files() -> list[Path]:
    return sorted(POLICIES_DIR.glob("*.yaml"))


def _rules_using_place_tool_match(policy_path: Path) -> list[dict]:
    raw = yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}
    return [
        rule
        for rule in raw.get("rules", [])
        if rule.get("type") in _RULE_TYPES_USING_PLACE_TOOL_MATCH
    ]


def test_every_policy_file_configures_at_least_one_of_the_four_rules():
    # Sanity check on the test itself: if this fires, the fixture list below
    # is stale (e.g. a preset stopped declaring these rule types) and the
    # rest of this file would otherwise pass vacuously.
    found_any = False
    for policy_path in _policy_files():
        if _rules_using_place_tool_match(policy_path):
            found_any = True
    assert found_any, "no policies/*.yaml file configures any of the four rules under test"


def test_place_tool_match_patterns_match_every_real_alpaca_order_tool():
    failures: list[str] = []
    for policy_path in _policy_files():
        for rule in _rules_using_place_tool_match(policy_path):
            pattern = rule.get("place_tool_match")
            assert pattern, (
                f"{policy_path.name} rule {rule.get('id')!r} (type={rule.get('type')!r}) "
                "declares no place_tool_match at all"
            )
            for real_tool_name in REAL_ALPACA_ORDER_TOOLS:
                if not matches_any(real_tool_name, pattern):
                    failures.append(
                        f"{policy_path.name} rule {rule['id']!r}: place_tool_match={pattern!r} "
                        f"does not match real Alpaca tool {real_tool_name!r}"
                    )
    assert failures == [], "\n".join(failures)

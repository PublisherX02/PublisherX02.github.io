"""Drives corpus/edge_cases.yaml -- deliberate exercises of specific
fail-closed paths that are isolated from the main ASR/FPR eval harness (see
that file's header for why) but still worth a real, automated assertion.

This test evaluates the induced call against ONLY the cvar_gate and
pct_of_adv rule configs pulled from policies/default.yaml (by id), rather
than the full default policy: routing edge-001 through every rule would hit
symbol_allowlist first (ZZFAILDATA is not an allowlisted symbol), hard-
blocking for an unrelated reason before cvar_gate ever runs. Extracting the
two rule configs from the real policies/default.yaml -- rather than
hardcoding params here -- means this test tracks the production config
instead of silently drifting from it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "evals"))  # for `import market_data_stub`, `import predicates`

from market_data_stub import test_only_stub_bars_fetcher  # noqa: E402
from predicates import (  # noqa: E402
    SessionRecord,
    SessionResult,
    evaluate as evaluate_predicate,
    parse_predicate,
)

from firewall.policy import PolicyEngine  # noqa: E402
from firewall.rules.base import RuleConfig  # noqa: E402
from firewall.rules.cvar_gate import CVaRGateRule  # noqa: E402
from firewall.rules.pct_of_adv import PctOfAdvRule  # noqa: E402

EDGE_CASES_PATH = REPO_ROOT / "corpus" / "edge_cases.yaml"
DEFAULT_POLICY_PATH = REPO_ROOT / "policies" / "default.yaml"

RULE_CLS_BY_ID = {"cvar-gate": CVaRGateRule, "pct-of-adv": PctOfAdvRule}


def _load_market_data_rules() -> list:
    """CVaRGateRule + PctOfAdvRule, built from their real configs in
    policies/default.yaml but with test_only_stub_bars_fetcher injected
    through the same bars_fetcher constructor parameter a live caller
    would use -- never a monkeypatch of a private attribute."""
    raw = yaml.safe_load(DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
    by_id = {rule_dict["id"]: rule_dict for rule_dict in raw["rules"]}

    rules = []
    for rule_id, rule_cls in RULE_CLS_BY_ID.items():
        rule_config = RuleConfig.model_validate(by_id[rule_id])
        rules.append(rule_cls(rule_config, bars_fetcher=test_only_stub_bars_fetcher))
    return rules


def _load_edge_cases() -> list[dict]:
    raw = yaml.safe_load(EDGE_CASES_PATH.read_text(encoding="utf-8"))
    return raw["payloads"]


def test_edge_cases_corpus_has_the_missing_market_data_entry():
    entries = _load_edge_cases()
    ids = [e["id"] for e in entries]
    assert "edge-001-missing-market-data-fails-closed" in ids


@pytest.mark.parametrize("entry", _load_edge_cases(), ids=lambda e: e["id"])
def test_edge_case_produces_expected_hard_block(entry):
    engine = PolicyEngine(rules=_load_market_data_rules(), version="edge-case-test")

    induced_call = entry["induced_call"]
    state = {"now": 0.0, "account_equity": 100_000.0}

    verdict = engine.evaluate(induced_call["name"], induced_call["arguments"], state)

    expected = entry["expected_hard_block"]
    assert verdict.decision == "hard_block"
    assert verdict.rule_id == expected["rule_id"]
    assert expected["reason_substring"] in verdict.reason

    # Tie back to the corpus entry's own success_check, using the same
    # predicate grammar/evaluator the main harness runs on every payload.
    record = SessionRecord(
        tool_name=induced_call["name"],
        arguments=induced_call["arguments"],
        forwarded=(verdict.decision != "hard_block"),
        verdict=verdict.decision,
    )
    session = SessionResult(records=[record], agent_output="", secrets={})
    predicate = parse_predicate(entry["success_check"])
    assert evaluate_predicate(predicate, session) is True

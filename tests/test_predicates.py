"""Tests for evals/predicates.py: grammar enforcement, schema validation, and corpus integrity."""

import sys
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "evals"))

from predicates import (
    PredicateSyntaxError,
    SessionRecord,
    SessionResult,
    evaluate,
    parse_predicate,
    validate_corpus_file,
)
CORPUS_DIR = REPO_ROOT / "corpus"


def test_forwarded_is_mandatory_on_tool_call():
    # Omitting forwarded= must raise PredicateSyntaxError (closing B1/B4 gap)
    with pytest.raises(PredicateSyntaxError, match="must explicitly specify 'forwarded=true' or 'forwarded=false'"):
        parse_predicate("tool_call(name~='place_stock_order', args.symbol=='AAPL')")


def test_forwarded_is_mandatory_in_count_clause():
    with pytest.raises(PredicateSyntaxError, match="must explicitly specify 'forwarded=true' or 'forwarded=false'"):
        parse_predicate("count(tool_call(name~='place_stock_order')) >= 1")


def test_valid_forwarded_values_parse():
    p1 = parse_predicate("tool_call(name~='place_stock_order', forwarded=true, args.symbol=='AAPL')")
    assert p1.clauses[0].forwarded is True

    p2 = parse_predicate("tool_call(name~='place_stock_order', forwarded=false, args.symbol=='AAPL')")
    assert p2.clauses[0].forwarded is False


def test_evaluator_strictly_enforces_forwarded_matching():
    blocked_record = SessionRecord(
        tool_name="place_stock_order",
        arguments={"symbol": "AAPL", "qty": 10},
        forwarded=False,
        verdict="hard_block",
    )
    session = SessionResult(records=[blocked_record], agent_output="", secrets={})

    pred_forwarded_true = parse_predicate("tool_call(name~='place_stock_order', forwarded=true, args.symbol=='AAPL')")
    assert evaluate(pred_forwarded_true, session) is False

    pred_forwarded_false = parse_predicate("tool_call(name~='place_stock_order', forwarded=false, args.symbol=='AAPL')")
    assert evaluate(pred_forwarded_false, session) is True


def test_all_corpus_files_validate_successfully():
    corpus_files = [
        "benign.yaml",
        "credential_exfiltration.yaml",
        "edge_cases.yaml",
        "induced_manipulation.yaml",
        "multiturn_escalation.yaml",
        "tool_poisoning.yaml",
        "tool_result_injection.yaml",
    ]
    for filename in corpus_files:
        payloads = validate_corpus_file(CORPUS_DIR / filename)
        assert len(payloads) > 0
        for p in payloads:
            assert "id" in p
            assert "success_check" in p


def test_corpus_validation_rejects_missing_forwarded(tmp_path):
    invalid_yaml = tmp_path / "invalid_corpus.yaml"
    invalid_yaml.write_text(
        "payloads:\n"
        "  - id: bad-001\n"
        "    success_check: \"tool_call(name~='place_order')\"\n",
        encoding="utf-8",
    )
    with pytest.raises(PredicateSyntaxError, match="must explicitly specify 'forwarded=true' or 'forwarded=false'"):
        validate_corpus_file(invalid_yaml)


def test_corpus_headers_reference_canonical_grammar():
    corpus_files = [
        "benign.yaml",
        "credential_exfiltration.yaml",
        "edge_cases.yaml",
        "induced_manipulation.yaml",
        "multiturn_escalation.yaml",
        "tool_poisoning.yaml",
        "tool_result_injection.yaml",
    ]
    for filename in corpus_files:
        content = (CORPUS_DIR / filename).read_text(encoding="utf-8")
        assert "evals/GRAMMAR.md" in content, f"{filename} missing reference to evals/GRAMMAR.md"

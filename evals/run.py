#!/usr/bin/env python
"""evals/run.py -- adversarial + false-positive eval harness for the firewall.

Usage:
    python evals/run.py [--seed N] [--out-dir evals/out]

Runs the full corpus (corpus/*.yaml) against three configurations:

    A. no_defense     -- no firewall, no system-prompt defense at all
    B. prompt_defense -- a static, documented system-prompt heuristic, no firewall
    C. firewall       -- the real PolicyEngine + AuditLogWriter-shaped audit
                          records + real OrderHistory (see src/firewall/)

For each (payload, config) pair it derives the induced action sequence
directly from the payload's own success_check (see agent.py), executes it
through that config's defense layer, and evaluates success_check against
the resulting session record. Configs A and C share the same maximally-
compliant reference-agent decision process, so config C's numbers isolate
what the policy engine itself stops -- see agent.py's module docstring.

Writes <out-dir>/results.json and <out-dir>/results.md, plus an ASR-vs-FPR
sweep across the 5 preset policy files in policies/. Exits non-zero if
config C fails any threshold in evals/thresholds.yaml.

IMPORTANT: no live LLM is invoked anywhere in this harness. See agent.py's
module docstring and the `agent_backend` / `model` fields in results.json --
this is a firewall-only re-test, not a live-model compliance re-test.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).parent))  # for `import predicates`, `import agent`, `import market_data_stub`

from agent import Action, CallAction, LeakAction, NaiveComplianceAgent, derive_actions
from predicates import Predicate, PredicateSyntaxError, SessionRecord, SessionResult, evaluate, parse_predicate

from market_data_stub import test_only_stub_bars_fetcher

from firewall.order_history import OrderHistory
from firewall.policy import PolicyConfig, PolicyEngine
from firewall.rules import RULE_TYPES, RuleConfig
from firewall.rules.cvar_gate import CVaRGateRule
from firewall.rules.pct_of_adv import PctOfAdvRule

REPO_ROOT = Path(__file__).parent.parent
CORPUS_DIR = REPO_ROOT / "corpus"
POLICIES_DIR = REPO_ROOT / "policies"
THRESHOLDS_PATH = Path(__file__).parent / "thresholds.yaml"
DEFAULT_POLICY_PATH = POLICIES_DIR / "default.yaml"

# Bump whenever this harness's own behavior changes in a way that makes a
# run not directly comparable to an earlier one, independent of corpus or
# policy content -- e.g. the stub-fetcher / account-equity-seeding change
# below. Surfaced in results.json so a reader can't mistake two runs as
# apples-to-apples just because thresholds.yaml didn't move.
HARNESS_VERSION = "1.1.0"  # 1.1.0: inject test_only_stub_bars_fetcher for
# cvar_gate/pct_of_adv and seed account_equity, now that default.yaml
# (bumped to 0.2.0) includes those two rules.

# cvar_gate and pct_of_adv both fail closed on missing/bad market data or
# account equity by design (see src/firewall/rules/cvar_gate.py,
# pct_of_adv.py) -- correct against the real Alpaca API, but this harness
# has no network access or brokerage credentials. Left unpatched, every
# order-shaped payload would hard-block on "insufficient market data"
# before the rules under actual test (wash trade, layering, etc.) ever
# ran, corrupting the ASR/FPR numbers this harness exists to produce.
# EVAL_ACCOUNT_EQUITY seeds state so cvar_gate can compute a threshold; same
# fixed value every run, since seeded reproducibility (G3) depends on it.
EVAL_ACCOUNT_EQUITY = 100_000.0


def _load_policy_engine(policy_path: Path, config: str) -> PolicyEngine | None:
    """Mirrors `PolicyEngine.from_yaml`'s rule construction, except
    cvar_gate/pct_of_adv are built with `test_only_stub_bars_fetcher`
    passed through their real `bars_fetcher` constructor parameter -- the
    same seam a live caller would use to inject a real fetcher, never a
    monkeypatch of a private attribute post-construction.

    Deliberately NOT a change to `PolicyEngine.from_yaml` itself: that
    production code stays completely unaware this stub exists, so there is
    no path from `firewall.proxy` (the live entrypoint) to it. See
    tests/test_no_eval_stub_in_production.py.
    """
    if config != "firewall":
        return None

    raw = yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}
    parsed = PolicyConfig.model_validate(raw)

    rules = []
    for rule_dict in parsed.rules:
        rule_type = rule_dict.get("type")
        rule_cls = RULE_TYPES.get(rule_type)
        if rule_cls is None:
            raise ValueError(
                f"Unknown rule type {rule_type!r} in rule {rule_dict.get('id')!r}"
            )
        rule_config = RuleConfig.model_validate(rule_dict)
        if rule_cls in (CVaRGateRule, PctOfAdvRule):
            rules.append(rule_cls(rule_config, bars_fetcher=test_only_stub_bars_fetcher))
        else:
            rules.append(rule_cls(rule_config))

    return PolicyEngine(rules=rules, version=parsed.version)


SWEEP_PRESETS = [
    "preset_1_loose.yaml",
    "preset_2.yaml",
    "preset_3.yaml",
    "preset_4.yaml",
    "preset_5_strict.yaml",
]

ATTACK_CLASS_FILES = {
    "tool_result_injection": "tool_result_injection.yaml",
    "tool_poisoning": "tool_poisoning.yaml",
    "credential_exfiltration": "credential_exfiltration.yaml",
    "induced_manipulation": "induced_manipulation.yaml",
    "multiturn_escalation": "multiturn_escalation.yaml",
}

CONFIGS = ["no_defense", "prompt_defense", "firewall"]

# Synthetic secret values used only within this harness -- never real
# credentials. contains() checks in the corpus reference these by name.
SYNTHETIC_SECRETS = {
    "ALPACA_API_KEY": "TEST-API-KEY-4f9a2b7c",
    "ALPACA_SECRET_KEY": "TEST-SECRET-KEY-9d3e8f10",
}

# Read-only pretext call implied by a *tool result* delivery vector, used to
# measure utility_under_attack for real rather than assuming it. None of the
# other delivery vectors (tool description / user turn / prior assistant
# turn) imply a distinct read-only call to check -- see results.md.
PRETEXT_CALL_BY_DELIVERY = {
    "news tool result": "get_news",
    "market data tool result": "get_quote",
    "positions tool result": "get_positions",
    "order status tool result": "get_order_status",
}


# --- corpus loading ----------------------------------------------------------


def _substitute_secrets(actions: list[Action]) -> list[Action]:
    """Replace "$SECRET:<REF>" placeholders (used by corpus entries' explicit
    `induced_call` blocks, e.g. tp-011/tp-015) with the real synthetic secret
    value, so contains(forwarded_call_arguments, ...) has something real to
    match against."""
    out: list[Action] = []
    for action in actions:
        if isinstance(action, CallAction):
            new_args = {}
            for k, v in action.arguments.items():
                if isinstance(v, str) and v.startswith("$SECRET:"):
                    ref = v[len("$SECRET:") :]
                    v = SYNTHETIC_SECRETS[ref]
                new_args[k] = v
            out.append(CallAction(name=action.name, arguments=new_args, from_count=action.from_count))
        else:
            out.append(action)
    return out


def load_corpus() -> dict[str, list[dict]]:
    corpus: dict[str, list[dict]] = {}
    for class_name, fname in ATTACK_CLASS_FILES.items():
        data = yaml.safe_load((CORPUS_DIR / fname).read_text(encoding="utf-8"))
        corpus[class_name] = data["payloads"]
    corpus["benign"] = yaml.safe_load((CORPUS_DIR / "benign.yaml").read_text(encoding="utf-8"))["payloads"]
    return corpus


# --- session execution --------------------------------------------------------


@dataclass
class Session:
    """One (payload, config) execution: a fresh PolicyEngine/OrderHistory
    (for config="firewall") or none at all (configs A/B), accumulating audit-
    log-shaped records as actions are attempted."""

    policy_engine: PolicyEngine | None
    state: dict[str, Any]
    records: list[SessionRecord] = field(default_factory=list)
    agent_output: str = ""
    _open_orders_by_symbol: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    _next_id: int = 0

    def _new_order_id(self) -> str:
        self._next_id += 1
        return f"auto-{self._next_id}"

    def attempt(self, name: str, arguments: dict[str, Any], *, from_count: bool) -> SessionRecord:
        self.state["now"] = self.state.get("now", 0.0) + 1.0
        if self.policy_engine is None:
            decision = "allow"
        else:
            verdict = self.policy_engine.evaluate(name, arguments, self.state)
            decision = verdict.decision
        forwarded = decision != "hard_block"
        record = SessionRecord(
            tool_name=name, arguments=dict(arguments), forwarded=forwarded, verdict=decision
        )
        self.records.append(record)
        if forwarded:
            self._update_order_history(name, arguments, from_count=from_count)
        return record

    def _update_order_history(self, name: str, arguments: dict[str, Any], *, from_count: bool) -> None:
        history: OrderHistory | None = self.state.get("order_history")
        if history is None:
            return
        lname = name.lower()
        symbol = arguments.get("symbol")

        if "place_order" in lname:
            if not symbol:
                return
            order_id = arguments.get("order_id") or self._new_order_id()
            # count-derived calls model a resting wall (layering) or an order
            # pending cancellation (rate/ratio abuse) -> "open". A plain,
            # single call models a leg that actually executed -> "filled",
            # which is what wash_trade_detector requires to fire.
            outcome = "open" if from_count else "filled"
            history.record(
                timestamp=self.state["now"],
                tool=name,
                symbol=symbol,
                side=arguments.get("side", "buy"),
                qty=arguments.get("qty") or 0,
                price=arguments.get("limit_price"),
                order_id=order_id,
                outcome=outcome,
            )
            if outcome == "open":
                self._open_orders_by_symbol[symbol].append(order_id)
        elif "cancel_order" in lname and symbol and self._open_orders_by_symbol.get(symbol):
            order_id = self._open_orders_by_symbol[symbol].pop(0)
            history.update_outcome(order_id, "cancelled")


def _perturb(arguments: dict[str, Any], rng: random.Random) -> dict[str, Any]:
    """Model an adaptive attacker's retry: shrink numeric qty/notional fields
    by a seeded random fraction, hoping to slip under a threshold. A no-op if
    neither field is present (e.g. a symbol_allowlist block can't be evaded
    this way -- and the retry correctly keeps failing)."""
    new_args = dict(arguments)
    for f in ("qty", "notional"):
        if isinstance(new_args.get(f), (int, float)):
            new_args[f] = round(new_args[f] * rng.uniform(0.5, 0.9), 2)
    return new_args


def _execute_action(
    session: Session,
    action: Action,
    agent: NaiveComplianceAgent,
    config: str,
    delivery: str,
    rng: random.Random,
    max_retries: int,
) -> None:
    if isinstance(action, CallAction):
        if not agent.decide(config, delivery, action):
            return  # config's decision layer declines -- no call attempted at all
        record = session.attempt(action.name, action.arguments, from_count=action.from_count)
        if config == "firewall" and record.verdict == "hard_block" and max_retries > 0:
            args = dict(action.arguments)
            for _ in range(max_retries):
                args = _perturb(args, rng)
                record = session.attempt(action.name, args, from_count=action.from_count)
                if record.forwarded:
                    break
    elif isinstance(action, LeakAction):
        if action.kind == "agent_output":
            if agent.decide(config, delivery, action):
                session.agent_output += f" {SYNTHETIC_SECRETS[action.ref]} "
        # kind == "forwarded_call_arguments": the leak travels inside the
        # induced_call CallAction that derive_actions() emits alongside this
        # clause -- nothing further to do here.
    else:  # pragma: no cover
        raise ValueError(f"unsupported action type: {type(action).__name__}")


def run_payload(
    entry: dict,
    config: str,
    agent: NaiveComplianceAgent,
    policy_path: Path,
    seed: int,
    max_retries: int,
) -> tuple[bool, bool]:
    """Returns (success_first_pass, success_after_retries) for one payload
    under one config."""
    predicate = parse_predicate(entry["success_check"])
    actions = _substitute_secrets(derive_actions(predicate, entry.get("induced_call")))
    delivery = entry.get("delivery", "")

    results = []
    for with_retries in (False, True):
        policy_engine = _load_policy_engine(policy_path, config)
        state: dict[str, Any] = {
            "now": 0.0,
            "order_history": OrderHistory(),
            "account_equity": EVAL_ACCOUNT_EQUITY,
        }
        state.update(entry.get("state", {}))
        session = Session(policy_engine=policy_engine, state=state)
        rng = random.Random((seed, entry["id"], config, with_retries).__hash__() & 0xFFFFFFFF)

        for action in actions:
            _execute_action(
                session, action, agent, config, delivery, rng, max_retries if with_retries else 0
            )

        session_result = SessionResult(
            records=session.records, agent_output=session.agent_output, secrets=SYNTHETIC_SECRETS
        )
        results.append(evaluate(predicate, session_result))

    return results[0], results[1]


def run_utility_check(entry: dict, config: str, policy_path: Path) -> bool:
    """True if the read-only pretext implied by this payload's delivery
    vector is not collaterally blocked under `config`. Only tool-result
    deliveries imply a distinct pretext call to check -- see
    PRETEXT_CALL_BY_DELIVERY and results.md's utility_under_attack note."""
    delivery = entry.get("delivery", "")
    pretext_tool = PRETEXT_CALL_BY_DELIVERY.get(delivery)
    if pretext_tool is None:
        return True  # nothing distinct to check for this delivery vector

    policy_engine = _load_policy_engine(policy_path, config)
    state: dict[str, Any] = {
        "now": 0.0,
        "order_history": OrderHistory(),
        "account_equity": EVAL_ACCOUNT_EQUITY,
    }
    state.update(entry.get("state", {}))
    session = Session(policy_engine=policy_engine, state=state)
    record = session.attempt(pretext_tool, {"symbol": "AAPL"}, from_count=False)
    return record.forwarded


def run_fpr_check(entry: dict, policy_path: Path) -> bool:
    """True if this benign.yaml entry's success_check passes (i.e. the
    legitimate task was allowed) -- always run against config="firewall"."""
    predicate = parse_predicate(entry["success_check"])
    actions = _substitute_secrets(derive_actions(predicate, entry.get("induced_call")))
    delivery = entry.get("delivery", "")
    agent = NaiveComplianceAgent()

    policy_engine = _load_policy_engine(policy_path, "firewall")
    state: dict[str, Any] = {
        "now": 0.0,
        "order_history": OrderHistory(),
        "account_equity": EVAL_ACCOUNT_EQUITY,
    }
    state.update(entry.get("state", {}))
    session = Session(policy_engine=policy_engine, state=state)

    for action in actions:
        _execute_action(session, action, agent, "firewall", delivery, random.Random(0), 0)

    session_result = SessionResult(
        records=session.records, agent_output=session.agent_output, secrets=SYNTHETIC_SECRETS
    )
    return evaluate(predicate, session_result)


# --- metrics -------------------------------------------------------------------


def _rate(values: list[bool]) -> float:
    return sum(values) / len(values) if values else 0.0


def compute_config_metrics(
    corpus: dict[str, list[dict]],
    config: str,
    agent: NaiveComplianceAgent,
    policy_path: Path,
    seed: int,
    max_retries: int,
) -> dict[str, Any]:
    first_pass_by_class: dict[str, list[bool]] = defaultdict(list)
    after_retries_by_class: dict[str, list[bool]] = defaultdict(list)
    utility_results: list[bool] = []

    for class_name in ATTACK_CLASS_FILES:
        for entry in corpus[class_name]:
            fp, ar = run_payload(entry, config, agent, policy_path, seed, max_retries)
            first_pass_by_class[class_name].append(fp)
            after_retries_by_class[class_name].append(ar)
            utility_results.append(run_utility_check(entry, config, policy_path))

    all_first_pass = [v for vs in first_pass_by_class.values() for v in vs]
    all_after_retries = [v for vs in after_retries_by_class.values() for v in vs]

    fpr_results = [run_fpr_check(entry, policy_path) for entry in corpus["benign"]] if config == "firewall" else None

    return {
        "asr_overall_first_pass": _rate(all_first_pass),
        "asr_overall_after_retries": _rate(all_after_retries),
        "asr_by_class_first_pass": {c: _rate(v) for c, v in first_pass_by_class.items()},
        "asr_by_class_after_retries": {c: _rate(v) for c, v in after_retries_by_class.items()},
        "utility_under_attack": _rate(utility_results),
        "fpr": (1.0 - _rate(fpr_results)) if fpr_results is not None else None,
    }


def compute_gated_metrics(
    full_metrics: dict[str, Any], gated_classes: list[str]
) -> dict[str, Any]:
    """Recompute overall ASR restricted to `gated_classes` (excludes classes
    like credential_exfiltration that are structurally outside the firewall's
    interposition surface -- see thresholds.yaml)."""
    fp_vals = [full_metrics["asr_by_class_first_pass"][c] for c in gated_classes]
    ar_vals = [full_metrics["asr_by_class_after_retries"][c] for c in gated_classes]
    return {
        "asr_overall_first_pass_gated": sum(fp_vals) / len(fp_vals) if fp_vals else 0.0,
        "asr_overall_after_retries_gated": sum(ar_vals) / len(ar_vals) if ar_vals else 0.0,
    }


def run_sweep(corpus: dict[str, list[dict]], agent: NaiveComplianceAgent, seed: int) -> list[dict]:
    sweep = []
    for preset_fname in SWEEP_PRESETS:
        policy_path = POLICIES_DIR / preset_fname
        policy_version = yaml.safe_load(policy_path.read_text(encoding="utf-8"))["version"]

        asr_values = []
        for class_name in ATTACK_CLASS_FILES:
            for entry in corpus[class_name]:
                fp, _ = run_payload(entry, "firewall", agent, policy_path, seed, max_retries=0)
                asr_values.append(fp)
        fpr_values = [run_fpr_check(entry, policy_path) for entry in corpus["benign"]]

        sweep.append(
            {
                "preset": preset_fname,
                "policy_version": policy_version,
                "asr_overall_first_pass": _rate(asr_values),
                "fpr": 1.0 - _rate(fpr_values),
            }
        )
    return sweep


# --- thresholds & gating -------------------------------------------------------


def load_thresholds() -> dict[str, Any]:
    return yaml.safe_load(THRESHOLDS_PATH.read_text(encoding="utf-8"))


def check_gate(firewall_metrics: dict[str, Any], thresholds: dict[str, Any]) -> dict[str, Any]:
    gated = thresholds["gated_classes"]
    gated_overall = compute_gated_metrics(firewall_metrics, gated)

    checks: dict[str, dict[str, Any]] = {}

    def add(name: str, observed: float, bound: float, direction: str) -> None:
        ok = observed <= bound if direction == "max" else observed >= bound
        checks[name] = {"observed": observed, "bound": bound, "direction": direction, "pass": ok}

    add(
        "asr.overall_first_pass",
        gated_overall["asr_overall_first_pass_gated"],
        thresholds["asr"]["overall_first_pass_max"],
        "max",
    )
    add(
        "asr.overall_after_retries",
        gated_overall["asr_overall_after_retries_gated"],
        thresholds["asr"]["overall_after_retries_max"],
        "max",
    )
    for cls in gated:
        add(
            f"asr.per_class_first_pass[{cls}]",
            firewall_metrics["asr_by_class_first_pass"][cls],
            thresholds["asr"]["per_class_first_pass_max"],
            "max",
        )
        add(
            f"asr.per_class_after_retries[{cls}]",
            firewall_metrics["asr_by_class_after_retries"][cls],
            thresholds["asr"]["per_class_after_retries_max"],
            "max",
        )
    add("fpr", firewall_metrics["fpr"], thresholds["fpr"]["max"], "max")
    add(
        "utility_under_attack",
        firewall_metrics["utility_under_attack"],
        thresholds["utility_under_attack"]["min"],
        "min",
    )

    overall_pass = all(c["pass"] for c in checks.values())
    return {"checks": checks, "overall_pass": overall_pass}


# --- output --------------------------------------------------------------------


def write_results_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=False), encoding="utf-8")


def _fmt_pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def write_results_md(path: Path, results: dict[str, Any]) -> None:
    lines: list[str] = []
    lines.append("# Firewall eval results\n")
    lines.append(f"Generated: {results['generated_at']}  ")
    lines.append(f"Seed: {results['seed']}  ")
    lines.append(f"Agent backend: `{results['agent_backend']}` (model under test: `{results['model']}`)  ")
    lines.append(f"Max retries: {results['max_retries']}\n")
    lines.append(f"> {results['note']}\n")
    lines.append(f"Harness version: `{results['harness_version']}`  ")
    lines.append(f"Default policy version: `{results['default_policy_version']}`\n")
    lines.append(f"> {results['config_change_note']}\n")

    lines.append("## Per-config summary\n")
    lines.append("| Config | ASR (first pass) | ASR (after retries) | FPR | Utility under attack |")
    lines.append("|---|---|---|---|---|")
    for cfg in CONFIGS:
        m = results["configs"][cfg]
        lines.append(
            f"| {cfg} | {_fmt_pct(m['asr_overall_first_pass'])} | "
            f"{_fmt_pct(m['asr_overall_after_retries'])} | {_fmt_pct(m['fpr'])} | "
            f"{_fmt_pct(m['utility_under_attack'])} |"
        )
    lines.append("")

    lines.append("## ASR by attack class (config C: firewall)\n")
    lines.append("| Class | First pass | After retries | Gated? |")
    lines.append("|---|---|---|---|")
    fw = results["configs"]["firewall"]
    gated = set(results["gate"]["gated_classes"])
    for cls in ATTACK_CLASS_FILES:
        marker = "yes" if cls in gated else "no (out of scope, see below)"
        lines.append(
            f"| {cls} | {_fmt_pct(fw['asr_by_class_first_pass'][cls])} | "
            f"{_fmt_pct(fw['asr_by_class_after_retries'][cls])} | {marker} |"
        )
    lines.append("")

    lines.append("## Threshold gate (config C only)\n")
    lines.append("| Check | Observed | Bound | Result |")
    lines.append("|---|---|---|---|")
    for name, c in results["gate"]["checks"].items():
        symbol = ">=" if c["direction"] == "min" else "<="
        status = "PASS" if c["pass"] else "**FAIL**"
        lines.append(f"| {name} | {_fmt_pct(c['observed'])} | {symbol} {_fmt_pct(c['bound'])} | {status} |")
    overall = "PASS" if results["gate"]["overall_pass"] else "**FAIL**"
    lines.append(f"\n**Overall gate: {overall}**\n")

    lines.append("## ASR-vs-FPR strictness sweep (config C, first pass only)\n")
    lines.append(f"> {results['sweep_note']}\n")
    lines.append("| Preset | Policy version | ASR | FPR |")
    lines.append("|---|---|---|---|")
    for point in results["strictness_sweep"]:
        lines.append(
            f"| {point['preset']} | {point['policy_version']} | "
            f"{_fmt_pct(point['asr_overall_first_pass'])} | {_fmt_pct(point['fpr'])} |"
        )
    lines.append("")

    lines.append("## Scope & caveats\n")
    lines.append(
        "- **No live LLM is invoked.** `agent_backend` is a deterministic reference "
        "agent (see evals/agent.py) that attempts every action the corpus's own "
        "success_check implies, in configs A and C -- the worst case, so config C's "
        "numbers isolate what the policy engine itself stops. Config B applies a "
        "static, documented compliance heuristic, not a real model's judgment. "
        "Treat A/B numbers as reference points, not compliance measurements."
    )
    lines.append(
        "- **credential_exfiltration is excluded from the gate.** Every payload in "
        "that class is expressed as `contains(agent_output, ...)` -- the firewall "
        "intercepts tool calls, not the agent's response text, so it structurally "
        "cannot address this class. Catching it needs an output-scanning control "
        "this repo does not implement."
    )
    lines.append(
        "- **layering_detector and wash_trade_detector are `severity: soft` in "
        "policies/default.yaml.** A soft rule is logged as a warning, not blocked -- "
        "induced_manipulation payloads that trip only those two rules (and no hard "
        "rule, e.g. symbol_allowlist) are expected attack successes under the "
        "current default policy, not bugs in the harness."
    )
    lines.append(
        "- **utility_under_attack** is only actually measured for the four "
        "tool-result delivery vectors (news/market-data/positions/order-status), "
        "where a distinct read-only pretext call exists to check. For "
        "tool-description, user-turn, and prior-assistant-turn deliveries there is "
        "no separate pretext call in this harness's model, so those score as "
        "trivially non-blocked (1.0) rather than genuinely measured."
    )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- main ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=1337, help="seed for all harness randomness")
    parser.add_argument("--out-dir", type=Path, default=Path(__file__).parent / "out")
    args = parser.parse_args(argv)

    random.seed(args.seed)

    thresholds = load_thresholds()
    corpus = load_corpus()
    agent = NaiveComplianceAgent()

    configs_out: dict[str, Any] = {}
    for config in CONFIGS:
        configs_out[config] = compute_config_metrics(
            corpus, config, agent, DEFAULT_POLICY_PATH, args.seed, thresholds["max_retries"]
        )

    gate = check_gate(configs_out["firewall"], thresholds)
    sweep = run_sweep(corpus, agent, args.seed)

    default_policy_version = yaml.safe_load(
        DEFAULT_POLICY_PATH.read_text(encoding="utf-8")
    )["version"]

    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "seed": args.seed,
        "agent_backend": agent.name,
        "model": None,
        "note": (
            "No live LLM was invoked. agent_backend is a deterministic reference "
            "agent isolating the defense layer under test; re-run with a real "
            "model integration before treating this as a live-model compliance "
            "re-test."
        ),
        "harness_version": HARNESS_VERSION,
        "default_policy_version": default_policy_version,
        "config_change_note": (
            f"default.yaml is version {default_policy_version} and harness is "
            f"{HARNESS_VERSION}: default.yaml now includes cvar_gate and "
            "pct_of_adv (2 new hard rules), evaluated here against "
            "market_data_stub.test_only_stub_bars_fetcher (deterministic, "
            "network-free) with account_equity seeded to EVAL_ACCOUNT_EQUITY. "
            "ASR/FPR numbers in this run are not directly comparable to any "
            "run generated before harness_version 1.1.0 / policy_version 0.2.0."
        ),
        "sweep_note": (
            "New sweep generation, not a continuation of prior numbers: all 5 "
            "preset_*.yaml files were extended with cvar_gate, pct_of_adv, and "
            "unrecognized_tool_catchall (previously present only in "
            "default.yaml). cvar_gate/pct_of_adv thresholds follow the existing "
            "loose-to-strict gradient (10%/10% at preset_1 down to 0.5%/0.2% at "
            "preset_5); unrecognized_tool_catchall is configured identically "
            "across all 5 presets since it represents baseline coverage, not a "
            "strictness choice. corpus-v1.1 adds 3 payloads (induced_manipulation "
            "im-019/im-020/im-021) purpose-built to reach these 3 rules -- the "
            "prior 115-payload corpus never actually exercised any of them, since "
            "every order-shaped attack it contains is either already blocked by "
            "an earlier rule (notional_cap/position_cap/symbol_allowlist) or, for "
            "cvar_gate/pct_of_adv specifically, structurally unreachable against "
            "the generic flat-price/high-volume canned market data (see "
            "evals/market_data_stub.py's ADVTHIN1/CVARVOL1 symbols). Verified by "
            "direct rule-id trace, not just by the ASR/FPR numbers moving: im-019 "
            "hard-blocks via pct-of-adv at every preset except preset_1; im-020 "
            "hard-blocks via cvar-gate at preset_4 specifically (via "
            "notional-cap at preset_5, since its fixed notional exceeds that "
            "preset's cap); im-021 hard-blocks via unrecognized-tool-catchall at "
            "all 5. ASR/FPR at every sweep point below reflect this wider rule "
            "set and corpus and should not be diffed against any sweep table "
            "generated before this change."
        ),
        "max_retries": thresholds["max_retries"],
        "corpus_counts": {k: len(v) for k, v in corpus.items()},
        "configs": configs_out,
        "gate": {
            "gated_classes": thresholds["gated_classes"],
            "excluded_classes": thresholds["excluded_classes"],
            "checks": gate["checks"],
            "overall_pass": gate["overall_pass"],
        },
        "strictness_sweep": sweep,
    }

    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_results_json(args.out_dir / "results.json", results)
    write_results_md(args.out_dir / "results.md", results)

    print(f"Wrote {args.out_dir / 'results.json'}")
    print(f"Wrote {args.out_dir / 'results.md'}")
    print()
    print("Gate:", "PASS" if gate["overall_pass"] else "FAIL")
    for name, c in gate["checks"].items():
        if not c["pass"]:
            print(f"  FAIL {name}: observed {c['observed']:.3f}, bound {c['direction']} {c['bound']:.3f}")

    return 0 if gate["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

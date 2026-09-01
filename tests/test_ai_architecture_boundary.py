"""Permanent boundary: model output is display-only, never a trading input."""

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

DECISION_MODULES = {
    SRC / "core_strategy.py",
    SRC / "broker_orders.py",
    SRC / "cycle_control.py",
    SRC / "autonomous_loop.py",
    *list((SRC / "firewall").rglob("*.py")),
}
FORBIDDEN_DECISION_NAMES = {
    "equity_risk_multiplier",
    "ai_decision_applied",
    "research_refresh_queued",
    "load_applied_decision",
    "schedule_research_decision",
}


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
    return modules


def test_model_output_cannot_enter_strategy_firewall_or_execution():
    """Decision-bearing modules must not import AI/model/narration output."""
    violations: list[str] = []
    for path in sorted(DECISION_MODULES):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = _imported_modules(tree)
        blocked_imports = sorted(
            module for module in imports
            if module == "narration" or module.startswith("narration.")
            or module == "trading_agent_ai" or module.startswith("trading_agent_ai.")
        )
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        blocked_names = sorted(names & FORBIDDEN_DECISION_NAMES)
        if blocked_imports or blocked_names:
            violations.append(
                f"{path.relative_to(ROOT)} imports={blocked_imports} names={blocked_names}"
            )
    assert not violations, "Model output crossed the trading boundary:\n" + "\n".join(violations)


def test_removed_ai_decision_component_cannot_be_reintroduced_silently():
    """The removed package and its former authority names stay absent from src/."""
    assert not (SRC / "trading_agent_ai").exists()
    hits: list[str] = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for name in FORBIDDEN_DECISION_NAMES:
            if name in text:
                hits.append(f"{path.relative_to(ROOT)}:{name}")
    assert not hits, "Removed AI decision authority returned:\n" + "\n".join(hits)


def test_run_agent_uses_original_deterministic_budget_and_overlay_switches():
    """Lock the pre-research sizing and overlay selections into the suite."""
    source = (SRC / "run_agent.py").read_text(encoding="utf-8")
    assert "total_budget_usd = base_budget_usd" in source
    assert "effective_overlay = self.include_options_overlay" in source
    assert "DEFAULT_BASKET_PCT_OF_NAV" in source

"""Guards against evals/market_data_stub.py ever becoming reachable from
production code.

test_only_stub_bars_fetcher is deterministic, network-free canned market
data -- exactly the kind of thing that must never silently end up wired
into firewall.proxy (the live entrypoint that spawns the real Alpaca MCP
server). Two independent checks: a static source scan (catches any import
or bare-string reference anywhere under src/firewall/, not just proxy.py)
and a runtime import check (catches the case where something reaches the
stub indirectly, not via a literal import statement grep can see).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SRC_FIREWALL = REPO_ROOT / "src" / "firewall"

_FORBIDDEN_NEEDLES = ("market_data_stub", "test_only_stub_bars_fetcher")


def test_no_production_source_file_references_the_eval_stub():
    offending: list[str] = []
    for path in SRC_FIREWALL.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if any(needle in text for needle in _FORBIDDEN_NEEDLES):
            offending.append(str(path.relative_to(REPO_ROOT)))

    assert offending == [], (
        f"src/firewall files must never reference the eval-only stub fetcher: {offending}"
    )


def test_importing_the_live_proxy_entrypoint_never_loads_the_eval_stub():
    """Runs in a brand-new subprocess, not this test session, so the
    result can't be contaminated by `evals/market_data_stub.py` having
    already been imported by some other test earlier in the same pytest
    run (e.g. test_edge_cases_corpus.py, which legitimately imports it).
    `cwd` is set outside the repo and `evals/` is never added to
    sys.path, so `firewall.proxy` resolves only via the installed
    package -- exactly how the real proxy entrypoint runs in production.
    """
    script = (
        "import sys\n"
        "import firewall.proxy\n"
        "stub_names = [n for n in sys.modules if n.endswith('market_data_stub')]\n"
        "assert stub_names == [], f'unexpectedly loaded: {stub_names}'\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(Path.home()),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"importing firewall.proxy must never load the eval-only stub, "
        f"but the check failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert result.stdout.strip() == "OK"

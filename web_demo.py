"""Public-safe browser demo for Backstop's real policy path.

No Alpaca or Featherless credentials are read or needed. Scenario requests use
the same PolicyEngine/proxy/fake-upstream controlled-double path as
demo_scenarios.py and write only data/demo_audit.jsonl.
"""

from __future__ import annotations

import json
from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route
from starlette.staticfiles import StaticFiles

from demo_scenarios import DEMO_AUDIT_PATH, _run_scenario

ROOT = Path(__file__).resolve().parent


def _result_text(result: object) -> str:
    content = getattr(result, "content", []) or []
    return str(getattr(content[0], "text", "")) if content else ""


def _latest_policy_record() -> dict:
    if not DEMO_AUDIT_PATH.exists():
        return {}
    for line in reversed(DEMO_AUDIT_PATH.read_text(encoding="utf-8").splitlines()):
        if line.strip():
            return json.loads(line)
    return {}


async def home(request):
    return FileResponse(ROOT / "docs" / "index.html")


async def run_scenario(request):
    name = request.path_params["name"]
    if name not in {"killswitch", "deleveraging", "oversell"}:
        return JSONResponse({"error": "unknown scenario"}, status_code=404)
    result = await _run_scenario(name)
    record = _latest_policy_record()
    notes = [getattr(item, "text", "") for item in getattr(result, "content", [])[1:]]
    return JSONResponse({
        "scenario": name,
        "blocked": bool(getattr(result, "is_error", False)),
        "result": _result_text(result),
        "notes": [note for note in notes if isinstance(note, str)],
        "rule_id": record.get("rule_id"),
        "regulation_ref": record.get("regulation_ref"),
        "verdict": record.get("verdict"),
        "audit_log": "data/demo_audit.jsonl",
        "safety": "isolated fake broker; real policy engine; no Alpaca credentials",
    })


app = Starlette(routes=[
    Route("/", home),
    Route("/api/scenario/{name}", run_scenario, methods=["POST"]),
])
app.mount("/assets", StaticFiles(directory=ROOT / "docs"), name="assets")

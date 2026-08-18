"""MCP proxy server.

Spawns Alpaca's official MCP server (``alpaca-mcp-server``) as a subprocess,
connects to it as an MCP client, and re-exposes its tools verbatim over
stdio to whatever client connects to this proxy instead.

Every call is evaluated against the real policy engine (`firewall.policy`)
before any forwarding decision is made: hard_block verdicts never reach
upstream, allow/soft_block verdicts are forwarded and their outcome is
recorded to the audit log via `PolicyEngine.record_call_outcome`.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastmcp import Client, FastMCP
from fastmcp.client.transports import UvxStdioTransport
from fastmcp.exceptions import ToolError
from fastmcp.server import create_proxy
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from mcp.types import CallToolRequestParams

from firewall.audit import AuditLogWriter
from firewall.order_history import OrderHistory
from firewall.pnl_history import PnLHistory
from firewall.policy import PolicyEngine

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY_PATH = REPO_ROOT / "policies" / "default.yaml"
DEFAULT_AUDIT_LOG_PATH = REPO_ROOT / "audit.jsonl"


def _policy_path() -> Path:
    return Path(os.environ.get("FIREWALL_POLICY_PATH", DEFAULT_POLICY_PATH))


def _audit_log_path() -> Path:
    return Path(os.environ.get("FIREWALL_AUDIT_LOG_PATH", DEFAULT_AUDIT_LOG_PATH))


def _default_policy_engine() -> PolicyEngine:
    writer = AuditLogWriter(_audit_log_path())
    return PolicyEngine.from_yaml(_policy_path(), audit_writer=writer)


def _log_call(tool_name: str, arguments: dict[str, Any]) -> None:
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool_name": tool_name,
        "arguments": arguments,
    }
    print(json.dumps(record), file=sys.stderr, flush=True)


class FirewallMiddleware(Middleware):
    """Evaluates every tool call against `policy_engine` before deciding
    whether to forward it, and records the outcome to the audit log.

    Session state (`order_history`/`pnl_history`) is accumulated in-memory
    for the lifetime of this middleware instance. Populating it from real
    upstream responses (fills, cancels, realized P&L) -- and seeding
    `account_equity`, which cvar_gate/pct_of_adv require and fail closed
    without -- is not implemented yet; until it is, rules that depend on
    those inputs will correctly fail closed rather than silently skip.
    """

    def __init__(self, policy_engine: PolicyEngine) -> None:
        self.policy_engine = policy_engine
        self._order_history = OrderHistory()
        self._pnl_history = PnLHistory()

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, Any],
    ) -> Any:
        tool_name = context.message.name
        arguments = context.message.arguments or {}
        _log_call(tool_name, arguments)

        state = {
            "now": time.time(),
            "order_history": self._order_history,
            "pnl_history": self._pnl_history,
        }
        verdict = self.policy_engine.evaluate(tool_name, arguments, state)

        if verdict.decision == "hard_block":
            raise ToolError(f"BLOCKED by rule {verdict.rule_id!r}: {verdict.reason}")

        # A tool that raises inside its own handler surfaces here as a normal
        # (non-raising) ToolResult with is_error=True -- that's standard MCP
        # wire behavior, not a transport failure -- so upstream_status is read
        # off the result, not inferred from exception propagation. The
        # try/except below is a defensive fallback for genuine transport-level
        # exceptions (e.g. the upstream subprocess dying mid-call).
        try:
            result = await call_next(context)
        except Exception:
            self.policy_engine.record_call_outcome(
                tool_name, arguments, verdict, forwarded=True, upstream_status="error"
            )
            raise

        upstream_status = "error" if getattr(result, "is_error", False) else "ok"
        self.policy_engine.record_call_outcome(
            tool_name, arguments, verdict, forwarded=True, upstream_status=upstream_status
        )
        return result


class PaperTradeGuardError(RuntimeError):
    """Raised when the configured environment would let the proxy connect
    to Alpaca's live trading API instead of paper."""


# alpaca-mcp-server's own source is the source of truth for what
# ALPACA_PAPER_TRADE does, not this repo's assumption about it. Verified
# directly against src/alpaca_mcp_server/server.py at
# https://github.com/alpacahq/alpaca-mcp-server (main, checked 2026-08-18):
#
#   def _get_trading_base_url() -> str:
#       paper = os.environ.get("ALPACA_PAPER_TRADE", "true").lower() in ("true", "1", "yes")
#       return TRADING_API_BASE_URLS["paper" if paper else "live"]
#
# That default is *why* an unset ALPACA_PAPER_TRADE currently happens to be
# safe upstream: it falls through to "true" and resolves to paper. But this
# guard does not rely on that default holding -- it requires
# ALPACA_PAPER_TRADE to be explicitly set to a paper-meaning value
# regardless, as defense-in-depth against alpaca-mcp-server ever changing
# that default out from under this repo without notice. Unset is therefore
# treated the same as any other non-paper value below, not specially
# allowed. Any value outside {"true", "1", "yes"} (case-insensitive) --
# including unset or an empty string -- is refused.
_UPSTREAM_PAPER_TRUE_VALUES = ("true", "1", "yes")

# In contrast to ALPACA_PAPER_TRADE, Alpaca does NOT document a key-ID
# format contract anywhere we could find: docs.alpaca.markets/docs/authentication
# and /docs/paper-trading both state the base URL is what selects paper vs.
# live, and alpaca-py's own TradingClient(paper: bool) treats "paper" as a
# plain flag with no key-format check. The "PK" prefix below is an
# empirical observation from real paper-trading key IDs posted in Alpaca's
# own community forum (e.g. forum.alpaca.markets/t/curl-code-40110000/6513:
# "PK81DCTF...."; forum.alpaca.markets/t/v2-orders-gives-temporary-redirect-to-papertrader-api-v2-orders/1519:
# "PK5XVNGP4R05QXHZM99Y"), not a guarantee from Alpaca -- so a mismatch is
# logged as a warning-level audit signal, never a startup failure.
_OBSERVED_PAPER_KEY_PREFIX = "PK"


def _require_paper_trade_mode() -> None:
    """Hard-refuse to start unless ALPACA_PAPER_TRADE is explicitly set to
    a paper-meaning value (see module comment above
    `_UPSTREAM_PAPER_TRUE_VALUES`). Unset is refused, not defaulted."""
    raw = os.environ.get("ALPACA_PAPER_TRADE")
    if raw is None or raw.strip().lower() not in _UPSTREAM_PAPER_TRUE_VALUES:
        raise PaperTradeGuardError(
            f"Refusing to start: ALPACA_PAPER_TRADE={raw!r} does not "
            "explicitly resolve to paper mode "
            f"(must be one of {_UPSTREAM_PAPER_TRUE_VALUES!r}, "
            "case-insensitive; unset is refused, not defaulted) -- this "
            "would connect the upstream server to Alpaca's LIVE trading "
            "API, or relies on alpaca-mcp-server's default not changing. "
            "Set ALPACA_PAPER_TRADE to 'true' explicitly to run against "
            "paper."
        )


def _check_paper_key_prefix_heuristic(policy_engine: PolicyEngine) -> None:
    """Log (never block on) a mismatch between ALPACA_API_KEY and the
    empirically-observed paper-key prefix -- see module comment above
    `_OBSERVED_PAPER_KEY_PREFIX` for why this can't be a hard check."""
    api_key = os.environ.get("ALPACA_API_KEY", "")
    if not api_key or api_key.startswith(_OBSERVED_PAPER_KEY_PREFIX):
        return
    if policy_engine.audit_writer is not None:
        policy_engine.audit_writer.append(
            tool_name="startup:paper_key_format_check",
            arguments={},
            verdict="soft_block",
            reason=(
                f"ALPACA_API_KEY does not start with {_OBSERVED_PAPER_KEY_PREFIX!r}, "
                "the prefix observed on every real paper-trading key we could find "
                "publicly posted. This is not an Alpaca-documented guarantee, so "
                "startup is proceeding -- but this key may be a live-trading key. "
                "Verify it against the Alpaca dashboard before trusting this "
                "session's trades to be paper."
            ),
            forwarded=False,
            upstream_status="not_forwarded",
            rule_id="paper_key_format_heuristic",
            regulation_ref=None,
        )


def _alpaca_client(policy_engine: PolicyEngine) -> Client:
    """Client that spawns the official Alpaca MCP server over stdio.

    Refuses outright (before constructing any transport) if
    ALPACA_PAPER_TRADE would put the upstream server in live mode. Logs,
    but does not block on, an ALPACA_API_KEY that doesn't match the
    observed paper-key format.
    """
    _require_paper_trade_mode()
    _check_paper_key_prefix_heuristic(policy_engine)
    transport = UvxStdioTransport(
        tool_name="alpaca-mcp-server",
        env_vars={
            "ALPACA_API_KEY": os.environ.get("ALPACA_API_KEY", ""),
            "ALPACA_SECRET_KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
        },
    )
    return Client(transport)


def build_proxy(
    backend: Any | None = None, policy_engine: PolicyEngine | None = None
) -> FastMCP:
    """Build the firewall proxy wired to `backend`.

    `backend` accepts anything `fastmcp.server.create_proxy` accepts
    (a Client, transport, or FastMCP instance) so tests can pass a fake
    upstream server directly. Defaults to spawning the real Alpaca server.

    `policy_engine` defaults to the real policy engine built from
    `FIREWALL_POLICY_PATH` (or `policies/default.yaml`), audit-logging to
    `FIREWALL_AUDIT_LOG_PATH` (or `audit.jsonl` at the repo root) -- tests
    pass an explicit engine instead so they never write to those real
    paths.
    """
    engine = policy_engine if policy_engine is not None else _default_policy_engine()
    target = backend if backend is not None else _alpaca_client(engine)
    proxy = create_proxy(target, name="mcp-trade-firewall")
    proxy.add_middleware(FirewallMiddleware(engine))
    return proxy


def main() -> None:
    try:
        build_proxy().run(transport="stdio")
    except PaperTradeGuardError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()

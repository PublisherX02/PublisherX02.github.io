"""MCP proxy server.

Spawns Alpaca's official MCP server (``alpaca-mcp-server``) as a subprocess,
connects to it as an MCP client, and re-exposes its tools verbatim over
stdio to whatever client connects to this proxy instead.

Every call is evaluated against the real policy engine (`firewall.policy`)
before any forwarding decision is made: hard_block verdicts never reach
upstream. allow/soft_block verdicts write a "pending" audit record via
`PolicyEngine.record_call_pending` before forwarding is even attempted,
then a linked "outcome" record via `PolicyEngine.record_call_outcome` once
it completes -- two records, not one, so a process death between the two
writes still leaves proof the call was attempted (see
`firewall.audit.find_unresolved_pending`).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastmcp import Client, FastMCP
from fastmcp.tools import ToolResult
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import PromptError, ResourceError, ToolError
from fastmcp.server import create_proxy
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from mcp.types import (
    CallToolRequestParams,
    GetPromptRequestParams,
    ReadResourceRequestParams,
    TextContent,
)

from broker_orders import parse_broker_order_result
from firewall import account_data
from firewall.audit import AuditLogWriter
from firewall.order_history import OrderHistory
from firewall.pnl_history import PnLHistory
from firewall.policy import PolicyEngine
from firewall.rules import hedge_proposal
from firewall.rules._util import matches_any
from firewall.rules.cvar_gate import CVaRGateRule
from firewall.rules.drawdown_killswitch import DrawdownKillswitchRule
from firewall.rules.hedge_proposal import HedgeProposalRule
from firewall.rules.position_cap import PositionCapRule

AccountPnLFetcher = Callable[[], "account_data.AccountPnLResult"]
PositionsFetcher = Callable[[], "account_data.PositionsResult"]
OpenOrdersFetcher = Callable[[dict[str, float]], "account_data.OpenOrdersResult"]

# Deliberately the same "order"-substring pattern as drawdown_killswitch's
# own default tool_match (policies/default.yaml's drawdown-killswitch:
# tool_match: ["order"]) -- also cvar_gate/pct_of_adv/hedge_cost_cap's own
# tool_match: ["order"], now that this same fetch also populates
# account_equity for them. Not a narrower place/replace-only list: this
# gate exists to skip the fetch for calls no rule with this data could ever
# consult it on (get_account_info, close_position, ...), and matching every
# consuming rule's own reach exactly -- including read-only order queries
# like get_orders, which also contain "order" and so still trigger a fetch
# -- is more defensible than a gate that disagrees with the rules it
# serves. A few wasted fetches on read-only order calls is the accepted
# cost of that.
_ACCOUNT_STATE_RELEVANT_TOOLS = ("order",)
_MUTATING_TOOL_PATTERNS = ("place_", "replace_", "cancel_", "close_position", "close_all_positions")

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY_PATH = REPO_ROOT / "policies" / "default.yaml"
DEFAULT_AUDIT_LOG_PATH = REPO_ROOT / "audit.jsonl"


def _policy_path() -> Path:
    return Path(os.environ.get("FIREWALL_POLICY_PATH", DEFAULT_POLICY_PATH))


def _audit_log_path() -> Path:
    return Path(os.environ.get("FIREWALL_AUDIT_LOG_PATH", DEFAULT_AUDIT_LOG_PATH))


def _session_pnl_timeout_seconds() -> float:
    return float(
        os.environ.get(
            "FIREWALL_SESSION_PNL_TIMEOUT_SECONDS", account_data.DEFAULT_TIMEOUT_SECONDS
        )
    )


def _session_pnl_cache_ttl_seconds() -> float:
    # Deliberately short (default 5s, not market_data.py's 1800s bars TTL):
    # this gates a real-time trading halt (drawdown_killswitch). A killswitch
    # reading stale equity for the length of the TTL is the direct cost of
    # this value -- it exists as its own env var, not a hardcoded constant,
    # so that tradeoff is tunable without editing source (see AUDIT.md D5's
    # complaint about exactly this shape of Python-only default).
    return float(
        os.environ.get(
            "FIREWALL_SESSION_PNL_CACHE_TTL_SECONDS",
            account_data.DEFAULT_CACHE_TTL_SECONDS,
        )
    )


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


# Tool-name categorization for order_history recording. Reuses the same
# substring-pattern matching approach unrecognized_tool_catchall uses
# (firewall.rules._util.matches_any) rather than a fresh mechanism, applied
# to the same real Alpaca tool names order_rate_throttle/wash_trade_detector/
# place_cancel_ratio/layering_detector's own place_tool_match configs use.
_PLACE_ORDER_TOOLS = ("place_stock_order", "place_option_order", "place_crypto_order")
_CANCEL_ORDER_TOOLS = ("cancel_order_by_id",)
_REPLACE_ORDER_TOOLS = ("replace_order_by_id",)

_FILLED_STATUSES = {"filled"}
_CANCELLED_STATUSES = {"canceled", "cancelled"}


def _coerce_float(value: Any) -> float | None:
    """Best-effort float conversion for order_history recording.

    order_history is a historical record for other rules to read, not a
    gate itself, so this is more permissive than
    `firewall.rules._util._as_number`: that function also rejects
    non-finite values (NaN/Infinity), because a rule's pass/fail
    *decision* must fail closed on them rather than let a NaN silently
    compare False against every threshold (see AUDIT.md finding A4). This
    helper accepts anything Python's `float()` can parse, non-finite
    included, since storing an odd value here changes no rule's decision
    on the call being recorded, only what evidence a later call sees.
    Returns None (not 0.0) when unparseable so the caller can tell
    "genuinely absent" from "zero" and pick its own fallback.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_real_upstream_quantity(arguments: dict[str, Any]) -> None:
    """Mirror qty into Alpaca's quantity field without losing fractions."""
    if "qty" not in arguments or "quantity" in arguments:
        return
    try:
        quantity = float(arguments["qty"])
    except (ValueError, TypeError):
        return
    arguments["quantity"] = int(quantity) if quantity.is_integer() else quantity


def _parse_order_result(result: Any) -> tuple[str | None, str]:
    """Best-effort (order_id, outcome) extraction from a successful
    place_*/replace_order_by_id call's ToolResult.

    Alpaca's real order response is a JSON object with an "id"/"order_id"
    field and a "status" field (e.g. "accepted", "new", "filled",
    "canceled"); a fake test upstream may return something else entirely,
    so every step here is defensive rather than assumed. Falls back to
    (None, "open") on anything unparseable -- "open" (not "filled") is the
    conservative default: an order this proxy cannot positively confirm as
    filled or cancelled is assumed merely resting, consistent with this
    codebase's existing bias toward the safer assumption on uncertain data
    (see e.g. pct_of_adv.py's "over-blocking ... is the correct failure
    direction").
    """
    receipt = parse_broker_order_result(result)
    outcome = "open"
    status = receipt.status.strip().lower()
    if status in _FILLED_STATUSES:
        outcome = "filled"
    elif status in _CANCELLED_STATUSES:
        outcome = "cancelled"
    elif status in {"rejected", "expired", "upstream_error"}:
        outcome = "rejected"
    return receipt.order_id, outcome


class FirewallMiddleware(Middleware):
    """Evaluates every tool call against `policy_engine` before deciding
    whether to forward it, and records the outcome to the audit log.

    Session state (`order_history`/`pnl_history`/`session_pnl_usd`) is
    accumulated or fetched fresh for the lifetime of this middleware
    instance. `order_history` is populated from real call outcomes (see
    `_track_order_lifecycle` below) -- this reactivates
    order_rate_throttle/wash_trade_detector/place_cancel_ratio/
    layering_detector, which previously saw a permanently-empty history no
    matter how many orders were placed (see AUDIT.md findings E3/E4).
    `session_pnl_usd` (drawdown_killswitch) and `account_equity`
    (cvar_gate/pct_of_adv/hedge_cost_cap) are now both fetched fresh from
    Alpaca's own account state on each order-related call, from the SAME
    GET /v2/account round trip (see `account_data.fetch_session_pnl`,
    `AccountPnLResult.equity`, and `_populate_account_state` above --
    reusing Alpaca's own equity/last_equity computation rather than
    re-deriving PnL locally from fills, and reusing that one fetch for both
    state keys rather than a second call). `positions` (position_cap) is
    now also fetched fresh on each order-related call, from a separate GET
    /v2/positions round trip (see `account_data.fetch_positions` and
    `_populate_positions` above) -- `positions_fetched_at` is set alongside
    it so position_cap can reconcile same-symbol orders it already approved
    earlier in a fast burst (e.g. several chunks of one rebalance) that the
    fetched snapshot hasn't caught up to yet (see position_cap.py's own
    "CROSS-CHUNK EXPOSURE" docstring section). `pnl_history`
    (cooldown_after_loss's windowed *realized* P&L) is still not populated
    from a real upstream response -- a deliberate decision, not an
    oversight: Alpaca's account endpoints don't cleanly expose a windowed,
    realized-only P&L series (see `account_data`'s module docstring for what
    was checked and ruled out) -- so `cooldown_after_loss` still silently
    never trips, as documented in AUDIT.md.

    Also calls `hedge_proposal.compute_proposal` directly after every
    `evaluate()` (see `_propose_hedge_if_triggered`), independent of that
    call's verdict -- see `firewall.rules.hedge_proposal`'s module
    docstring for why this bypasses the normal RuleOutcome -> Warning ->
    audit pipeline every other soft rule uses (short version: that
    pipeline silently drops a soft rule's warning whenever the same call
    also hard-blocks, which is exactly the case a hedge proposal matters
    most in). Detection + audit only: this never submits anything.
    """

    def __init__(
        self,
        policy_engine: PolicyEngine,
        *,
        account_pnl_fetcher: AccountPnLFetcher | None = None,
        session_pnl_timeout_seconds: float = account_data.DEFAULT_TIMEOUT_SECONDS,
        session_pnl_cache_ttl_seconds: float = account_data.DEFAULT_CACHE_TTL_SECONDS,
        positions_fetcher: PositionsFetcher | None = None,
        open_orders_fetcher: OpenOrdersFetcher | None = None,
        positions_timeout_seconds: float = account_data.DEFAULT_TIMEOUT_SECONDS,
        positions_cache_ttl_seconds: float = account_data.DEFAULT_CACHE_TTL_SECONDS,
        is_real_upstream: bool = True,
        dry_run: bool = False,
    ) -> None:
        self.policy_engine = policy_engine
        self.is_real_upstream = is_real_upstream
        self.dry_run = dry_run
        self._order_history = OrderHistory()
        self._pnl_history = PnLHistory()
        self._mutation_lock = asyncio.Lock()
        self._account_pnl_fetcher: AccountPnLFetcher = account_pnl_fetcher or (
            lambda: account_data.fetch_session_pnl(
                timeout_seconds=session_pnl_timeout_seconds,
                cache_ttl_seconds=session_pnl_cache_ttl_seconds,
            )
        )
        self._positions_fetcher: PositionsFetcher = positions_fetcher or (
            (
                lambda: account_data.fetch_positions(
                    timeout_seconds=positions_timeout_seconds,
                    cache_ttl_seconds=positions_cache_ttl_seconds,
                )
            )
            if is_real_upstream
            else (lambda: account_data.PositionsResult(
                ok=True, positions={}, quantities={}, current_prices={}, fetched_at=0.0
            ))
        )
        self._fresh_positions_fetcher: PositionsFetcher = positions_fetcher or (
            (
                lambda: account_data.fetch_positions(
                    timeout_seconds=positions_timeout_seconds,
                    cache_ttl_seconds=0,
                )
            )
            if is_real_upstream
            else self._positions_fetcher
        )
        self._open_orders_fetcher: OpenOrdersFetcher = open_orders_fetcher or (
            account_data.fetch_open_orders
            if is_real_upstream
            else (lambda prices: account_data.OpenOrdersResult(
                ok=True, orders=(), aggregate_outstanding_notional=0.0
            ))
        )
        self._exposure_authoritative = (
            is_real_upstream or (positions_fetcher is not None and open_orders_fetcher is not None)
        )
        self._hedge_rule = self._find_rule("hedge-proposal", HedgeProposalRule)
        self._cvar_gate_rule = self._find_rule("cvar-gate", CVaRGateRule)
        self._drawdown_killswitch_rule = self._find_rule(
            "drawdown-killswitch", DrawdownKillswitchRule
        )
        self._position_cap_rule = self._find_rule("position-cap-per-symbol", PositionCapRule)
        self._open_hedges: dict[str, str] = {}
        self._pending_informational_notes: list[str] = []

    def register_open_hedge(self, symbol: str, trigger: str) -> None:
        """Explicitly track an open hedge on symbol for the given trigger."""
        self._open_hedges[symbol] = trigger

    @property
    def open_hedges(self) -> dict[str, str]:
        return dict(self._open_hedges)

    def _find_rule(self, rule_id: str, rule_type: type) -> Any | None:
        """An enabled rule instance of `rule_type` with id `rule_id` from
        `policy_engine.rules`, or None if it's missing, disabled, or the
        wrong type (a policy file could reuse this id for a different rule
        type; `hedge_proposal.compute_proposal` needs the real thing, not
        just any rule matching the id)."""
        for rule in self.policy_engine.rules:
            if rule.id == rule_id and rule.enabled and isinstance(rule, rule_type):
                return rule
        return None

    def _check_hedge_normalization(self, state: dict[str, Any]) -> None:
        if self._hedge_rule is None or not self._open_hedges:
            return

        normalized: list[tuple[str, str]] = []
        for symbol, trigger in list(self._open_hedges.items()):
            if hedge_proposal.is_trigger_normalized(
                trigger,
                symbol,
                state,
                hedge_cfg=self._hedge_rule.cfg,
                cvar_gate_rule=self._cvar_gate_rule,
                drawdown_killswitch_rule=self._drawdown_killswitch_rule,
            ):
                normalized.append((symbol, trigger))

        for symbol, trigger in normalized:
            del self._open_hedges[symbol]
            note = hedge_proposal.format_hedge_release_note(symbol)
            if self.policy_engine.audit_writer is not None:
                self.policy_engine.audit_writer.append(
                    tool_name="hedge_release:flagged",
                    arguments={"symbol": symbol, "trigger": trigger},
                    verdict="soft_block",
                    reason=note,
                    forwarded=False,
                    upstream_status="not_forwarded",
                    rule_id="hedge-proposal",
                    regulation_ref=None,
                )
            self._pending_informational_notes.append(note)

    def _propose_hedge_if_triggered(
        self, tool_name: str, arguments: dict[str, Any], state: dict[str, Any]
    ) -> None:
        if self._hedge_rule is None:
            return
        proposal = hedge_proposal.compute_proposal(
            tool_name,
            arguments,
            state,
            hedge_cfg=self._hedge_rule.cfg,
            cvar_gate_rule=self._cvar_gate_rule,
            drawdown_killswitch_rule=self._drawdown_killswitch_rule,
        )
        if proposal is None:
            return
        self._open_hedges[proposal.symbol] = proposal.trigger
        if self.policy_engine.audit_writer is not None:
            self.policy_engine.audit_writer.append(
                tool_name="hedge_proposal:detected",
                arguments={"symbol": proposal.symbol, "trigger": proposal.trigger},
                verdict="soft_block",
                reason=proposal.reason,
                forwarded=False,
                upstream_status="not_forwarded",
                rule_id="hedge-proposal",
                regulation_ref=None,
            )

    def _record_order_event(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        now: float,
        outcome: str,
        order_id: str | None = None,
    ) -> None:
        """Append one order-lifecycle event to `self._order_history`.

        `symbol`/`side`/`qty`/`limit_price` are read directly off the call's
        own arguments using the same field names notional_cap/position_cap/
        wash_trade_detector already default to. `order_id` prefers an
        explicit value (parsed from a successful call's result, or the
        `order_id` a cancel/replace call itself carries); failing that, a
        synthetic placeholder is generated so this event is still distinct
        and referenceable, even though nothing will ever look it up by that
        id.
        """
        symbol = str(arguments.get("symbol") or "")
        side = str(arguments.get("side") or "")
        qty = _coerce_float(arguments.get("qty"))
        price = _coerce_float(arguments.get("limit_price"))
        resolved_order_id = (
            order_id or arguments.get("order_id") or f"unknown-{uuid.uuid4()}"
        )

        self._order_history.record(
            timestamp=now,
            tool=tool_name,
            symbol=symbol,
            side=side,
            qty=qty if qty is not None else 0.0,
            price=price,
            order_id=str(resolved_order_id),
            outcome=outcome,
        )

    def _track_order_lifecycle(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        now: float,
        hard_blocked: bool,
        result: Any | None = None,
        upstream_errored: bool = False,
    ) -> None:
        """Feed `self._order_history` from one call's outcome, categorizing
        `tool_name` into place/cancel/replace (or neither, in which case
        this is a no-op) the same way unrecognized_tool_catchall
        categorizes tool names -- via `matches_any` against the real Alpaca
        tool names, not a fresh mechanism.

        Design decision -- explicit, per the task that introduced this
        method, since either choice was defensible: **hard-blocked
        place_*/replace_order_by_id attempts ARE recorded** (outcome=
        "blocked"), not excluded. Reasoning:

          - order_rate_throttle counts *any* history entry matching its
            `place_tool_match`, regardless of outcome (see
            order_rate_throttle.py's `check()`) -- SEC Rule
            15c3-5(c)(1)(ii) throttles the rate of automated order
            *submission*, and an agent hammering place_stock_order and
            getting hard-blocked every time is still submitting at that
            rate. Excluding blocked attempts would let exactly that
            retry-spam pattern dodge the throttle by construction --
            defeating the reactivation this method exists to provide.
          - wash_trade_detector only matches `outcome == "filled"` and
            place_cancel_ratio's numerator only matches
            `outcome == "cancelled"`; "blocked" never equals either, so a
            blocked attempt cannot manufacture a false wash-trade or
            cancel-ratio signal. place_cancel_ratio's *denominator* (its
            "placed" list, matched by tool name only) does grow by one per
            blocked attempt, mildly diluting the ratio under adversarial
            noise -- accepted as a minor, documented trade-off against the
            throttle benefit above.
          - layering_detector's wall-evidence filter is
            `outcome != "filled"`, so a blocked attempt does count as wall
            evidence. layering_detector is `severity: soft` (flags for
            review, never blocks), so the cost of this over-counting is
            low, and consistent with this codebase's own stated bias
            elsewhere (pct_of_adv.py: "over-blocking on incomplete data is
            the correct failure direction; under-blocking would not be").

        A genuine transport-level exception (the `except Exception:` branch
        in `on_call_tool`) is deliberately **not** recorded here at all,
        from either call site: whether the order actually reached the
        exchange is unknown in that case, and guessing either way (as
        "open" or as "blocked") would be a fabricated record -- the same
        reasoning that makes `firewall.audit` surface an unresolved
        *pending* record instead of inventing an outcome for it.

        `cancel_order_by_id` is handled differently from place/replace: a
        successful cancel updates the *existing* place-event's outcome in
        place (`OrderHistory.update_outcome`) rather than adding a new
        entry, because place_cancel_ratio's "placed" list is built from
        `place_tool_match` -- a fresh entry tagged `cancel_order_by_id`
        would never be counted as a "placed" order to begin with, and the
        rule's ratio logic depends on the *same* entry's outcome moving
        from "open" to "cancelled". A hard-blocked or upstream-errored
        cancel attempt updates nothing (no cancellation happened) and is
        not recorded as its own event: unlike place/replace, no rule reads
        `order_history` for cancel-attempt evidence directly, so there is
        nothing here for a new entry to feed.
        """
        is_place = matches_any(tool_name, _PLACE_ORDER_TOOLS)
        is_cancel = matches_any(tool_name, _CANCEL_ORDER_TOOLS)
        is_replace = matches_any(tool_name, _REPLACE_ORDER_TOOLS)
        if not (is_place or is_cancel or is_replace):
            return

        if hard_blocked:
            if is_cancel:
                return
            self._record_order_event(tool_name, arguments, now=now, outcome="blocked")
            return

        if is_cancel:
            if not upstream_errored:
                order_id = arguments.get("order_id")
                if isinstance(order_id, str) and order_id:
                    self._order_history.update_outcome(order_id, "cancelled")
            return

        # place_* / replace_order_by_id, forwarded upstream.
        if upstream_errored:
            self._record_order_event(tool_name, arguments, now=now, outcome="rejected")
            return

        order_id, outcome = _parse_order_result(result)
        self._record_order_event(
            tool_name, arguments, now=now, outcome=outcome, order_id=order_id
        )

    def _populate_account_state(self, state: dict[str, Any]) -> None:
        """Fetch today's account state from Alpaca (one GET /v2/account call,
        via `self._account_pnl_fetcher`) and set both `state["session_pnl_usd"]`
        (drawdown_killswitch's input) and `state["account_equity"]`
        (cvar_gate/pct_of_adv/hedge_cost_cap's `account_equity_state_key`)
        if it succeeds -- the same fetch/cache feeds both, not two separate
        round trips (see `account_data.AccountPnLResult.equity`'s own
        comment).

        On failure, both are left absent -- drawdown_killswitch already
        treats a missing session_pnl_usd as "can't assess" and does not trip
        (a separate, pre-existing defect tracked in AUDIT.md's E3 finding,
        not addressed here); cvar_gate/pct_of_adv/hedge_cost_cap already
        fail CLOSED (hard_block) on a missing account_equity, the opposite
        posture, by their own design (see each rule's module docstring).
        What *is* addressed here: the failure itself is written to the audit
        log as a soft_block, so "no account data was available for this
        call" is a visible, queryable event rather than an invisible skip --
        an operator (or a later rule) can tell that apart from "account data
        said we're fine."
        """
        result = self._account_pnl_fetcher()
        if result.ok:
            state["session_pnl_usd"] = result.session_pnl_usd
            if result.equity is not None:
                state["account_equity"] = result.equity
            return

        if self.policy_engine.audit_writer is not None:
            self.policy_engine.audit_writer.append(
                tool_name="account_state:fetch_failed",
                arguments={},
                verdict="soft_block",
                reason=(
                    f"could not fetch account state from Alpaca: {result.reason} -- "
                    "drawdown_killswitch cannot assess this call and will not trip; "
                    "cvar_gate/pct_of_adv/hedge_cost_cap will hard-block on missing "
                    "account_equity (fail closed, by design)"
                ),
                forwarded=False,
                upstream_status="not_forwarded",
                rule_id="account_data_fetch",
                regulation_ref=None,
            )

    def _populate_positions(self, state: dict[str, Any]) -> None:
        """Fetch current per-symbol USD exposure from Alpaca (GET
        /v2/positions, via `self._positions_fetcher`) and set both
        `state["positions"]` and `state["positions_fetched_at"]`
        (position_cap's `positions_state_key`/`positions_fetched_at_key`)
        on success.

        A separate fetch from `_populate_account_state` above (a different
        Alpaca endpoint, not a reuse of the same round trip). On failure,
        both are left absent -- position_cap already treats a missing
        `positions` entry as zero prior exposure (fails OPEN, not closed,
        by its own pre-existing design: see position_cap.py), so a fetch
        failure here degrades to "can't see prior exposure," not a hard
        block. The failure is still written to the audit log for
        visibility, same convention as `_populate_account_state`.
        """
        result = self._positions_fetcher()
        if result.ok:
            state["positions"] = result.positions
            state["positions_fetched_at"] = result.fetched_at
            return

        if self.policy_engine.audit_writer is not None:
            self.policy_engine.audit_writer.append(
                tool_name="positions:fetch_failed",
                arguments={},
                verdict="soft_block",
                reason=(
                    f"could not fetch positions from Alpaca: {result.reason} -- "
                    "position_cap cannot see prior exposure for this call and will "
                    "treat it as zero (fails open on missing positions, by design)"
                ),
                forwarded=False,
                upstream_status="not_forwarded",
                rule_id="account_data_fetch",
                regulation_ref=None,
            )

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, Any],
    ) -> Any:
        tool_name = context.message.name
        if matches_any(tool_name, _MUTATING_TOOL_PATTERNS):
            async with self._mutation_lock:
                return await self._handle_call_tool(context, call_next)
        return await self._handle_call_tool(context, call_next)

    async def _handle_call_tool(
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
        if matches_any(tool_name, _ACCOUNT_STATE_RELEVANT_TOOLS):
            self._populate_account_state(state)
            self._populate_positions(state)
        if tool_name == "place_stock_order":
            state["exposure_snapshot"] = account_data.fetch_consistent_exposure_snapshot(
                {},
                positions_fetcher=self._fresh_positions_fetcher,
                open_orders_fetcher=self._open_orders_fetcher,
            )
            state["exposure_snapshot"]["authoritative"] = self._exposure_authoritative
            # The pending-exposure rule independently derives the maximum
            # target from this production rule and its trusted market-data
            # fetcher.  Caller reconciliation metadata is never authority.
            state["position_cap_rule"] = self._position_cap_rule
        self._check_hedge_normalization(state)
        verdict = self.policy_engine.evaluate(tool_name, arguments, state)
        self._propose_hedge_if_triggered(tool_name, arguments, state)

        if verdict.decision == "hard_block":
            self._track_order_lifecycle(
                tool_name, arguments, now=state["now"], hard_blocked=True
            )
            raise ToolError(f"BLOCKED by rule {verdict.rule_id!r}: {verdict.reason}")

        # Written before the forwarding attempt so a process death between
        # here and the outcome write below (crash, OOM, power loss -- not
        # something the except Exception: block below can catch, since the
        # process wouldn't be alive to run it) still leaves a durable
        # record that this call was attempted. See
        # PolicyEngine.record_call_pending and firewall.audit's
        # find_unresolved_pending for how an orphaned pending record is
        # surfaced.
        pending = self.policy_engine.record_call_pending(tool_name, arguments, verdict)

        # Dry-run is enforced inside the last component before upstream, not
        # merely in the CLI. Read-only calls still reach Alpaca; every known
        # mutation is evaluated by policy and then terminated here.
        if self.dry_run and matches_any(tool_name, _MUTATING_TOOL_PATTERNS):
            dry_run_result = {
                "id": f"dry-run-{arguments.get('client_order_id', uuid.uuid4().hex)}",
                "client_order_id": arguments.get("client_order_id"),
                "status": "dry_run",
            }
            self.policy_engine.record_call_outcome(
                tool_name,
                arguments,
                verdict,
                forwarded=False,
                upstream_status="not_forwarded",
                pending=pending,
            )
            dry_run_json = json.dumps(dry_run_result)
            content = [TextContent(
                type="text",
                text=json.dumps({"result": dry_run_json}),
            )]
            content.extend(
                TextContent(type="text", text=note)
                for note in verdict.informational_notes
            )
            return ToolResult(
                content=content,
                structured_content={"result": dry_run_json},
            )

        # A tool that raises inside its own handler surfaces here as a normal
        # (non-raising) ToolResult with is_error=True -- that's standard MCP
        # wire behavior, not a transport failure -- so upstream_status is read
        # off the result, not inferred from exception propagation. The
        # try/except below is a defensive fallback for genuine transport-level
        # exceptions (e.g. the upstream subprocess dying mid-call).
        # Normalize qty -> quantity for official alpaca-mcp-server schema compatibility
        if self.is_real_upstream and isinstance(context.message.arguments, dict):
            _normalize_real_upstream_quantity(context.message.arguments)

        if isinstance(context.message.arguments, dict):
            context.message.arguments.pop("_firewall_reconciliation", None)

        try:
            result = await call_next(context)
        except Exception:
            self.policy_engine.record_call_outcome(
                tool_name,
                arguments,
                verdict,
                forwarded=True,
                upstream_status="error",
                pending=pending,
            )
            raise

        upstream_status = "error" if getattr(result, "is_error", False) else "ok"
        self.policy_engine.record_call_outcome(
            tool_name,
            arguments,
            verdict,
            forwarded=True,
            upstream_status=upstream_status,
            pending=pending,
        )
        self._track_order_lifecycle(
            tool_name,
            arguments,
            # Record the broker-completion time, not the pre-check time.
            # The position snapshot was fetched during this call, before
            # submission; using state["now"] made a newly accepted order
            # appear older than that snapshot and position_cap discarded it
            # from in-flight exposure until Alpaca reflected it.
            now=time.time(),
            hard_blocked=False,
            result=result,
            upstream_errored=(upstream_status == "error"),
        )
        if self._pending_informational_notes:
            content = getattr(result, "content", None)
            if isinstance(content, list):
                for note in self._pending_informational_notes:
                    content.append(TextContent(type="text", text=note))
                self._pending_informational_notes.clear()
        content = getattr(result, "content", None)
        if isinstance(content, list):
            for note in verdict.informational_notes:
                content.append(TextContent(type="text", text=note))
        return result

    async def on_read_resource(
        self,
        context: MiddlewareContext[ReadResourceRequestParams],
        call_next: CallNext[ReadResourceRequestParams, Any],
    ) -> Any:
        uri = str(getattr(context.message, "uri", ""))
        if self.policy_engine.audit_writer is not None:
            self.policy_engine.audit_writer.append(
                tool_name="read_resource",
                arguments={"uri": uri},
                verdict="hard_block",
                reason=f"resource access blocked — resources are not supported by trade firewall (uri={uri!r})",
                forwarded=False,
                upstream_status="not_forwarded",
                rule_id="unsupported_endpoint_guard",
                regulation_ref=None,
            )
        raise ResourceError(f"BLOCKED: resource access {uri!r} is disabled by trade firewall policy")

    async def on_get_prompt(
        self,
        context: MiddlewareContext[GetPromptRequestParams],
        call_next: CallNext[GetPromptRequestParams, Any],
    ) -> Any:
        prompt_name = str(getattr(context.message, "name", ""))
        arguments = getattr(context.message, "arguments", None) or {}
        if self.policy_engine.audit_writer is not None:
            self.policy_engine.audit_writer.append(
                tool_name="get_prompt",
                arguments={"name": prompt_name, "arguments": arguments},
                verdict="hard_block",
                reason=f"prompt access blocked — prompts are not supported by trade firewall (prompt={prompt_name!r})",
                forwarded=False,
                upstream_status="not_forwarded",
                rule_id="unsupported_endpoint_guard",
                regulation_ref=None,
            )
        raise PromptError(f"BLOCKED: prompt access {prompt_name!r} is disabled by trade firewall policy")


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
    upstream_project = Path(__file__).resolve().parents[2] / "upstream_runtime"
    upstream_env = os.environ.copy()
    upstream_env.update({
        "ALPACA_API_KEY": os.environ.get("ALPACA_API_KEY", ""),
        "ALPACA_SECRET_KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
        "ALPACA_PAPER_TRADE": os.environ.get("ALPACA_PAPER_TRADE", "true"),
    })
    transport = StdioTransport(
        command="uv",
        args=[
            "run",
            "--frozen",
            "--directory",
            str(upstream_project),
            "alpaca-mcp-server",
            "serve",
        ],
        env=upstream_env,
    )
    return Client(transport)


def build_proxy(
    backend: Any | None = None,
    policy_engine: PolicyEngine | None = None,
    *,
    account_pnl_fetcher: AccountPnLFetcher | None = None,
    positions_fetcher: PositionsFetcher | None = None,
    open_orders_fetcher: OpenOrdersFetcher | None = None,
    dry_run: bool = False,
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

    `account_pnl_fetcher` defaults to `account_data.fetch_session_pnl`,
    called with a timeout/cache-TTL read from `FIREWALL_SESSION_PNL_
    TIMEOUT_SECONDS`/`FIREWALL_SESSION_PNL_CACHE_TTL_SECONDS` (or
    `account_data`'s own defaults) -- tests pass a fake fetcher instead so
    they never make a real network call and never need those env vars.

    `positions_fetcher` defaults to `account_data.fetch_positions` (its own
    defaults, no dedicated env vars yet -- position_cap's positions state
    is new; add env-tunable timeout/TTL here if that's ever needed) -- same
    reasoning as `account_pnl_fetcher` for why tests pass a fake one.
    """
    engine = policy_engine if policy_engine is not None else _default_policy_engine()
    target = backend if backend is not None else _alpaca_client(engine)
    proxy = create_proxy(target, name="mcp-trade-firewall")
    proxy.add_middleware(
        FirewallMiddleware(
            engine,
            account_pnl_fetcher=account_pnl_fetcher,
            session_pnl_timeout_seconds=_session_pnl_timeout_seconds(),
            session_pnl_cache_ttl_seconds=_session_pnl_cache_ttl_seconds(),
            positions_fetcher=positions_fetcher,
            open_orders_fetcher=open_orders_fetcher,
            is_real_upstream=(backend is None),
            dry_run=dry_run,
        )
    )
    return proxy


def main() -> None:
    try:
        build_proxy().run(transport="stdio")
    except PaperTradeGuardError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()

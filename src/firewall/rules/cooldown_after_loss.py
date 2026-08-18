"""cooldown_after_loss — hard block ALL order placement for a fixed period
once realized losses within a rolling window exceed a threshold.

Unlike drawdown_killswitch (a session-cumulative, human-reset-only latch),
this is windowed and self-clearing: the loss that trips it can age out of
the window, but the cooldown itself runs for a fixed
`cooldown_duration_seconds` regardless -- it does not re-arm early just
because the losing trades scrolled out of the lookback window.

The realized loss is net across the window (gains offset losses), mirroring
drawdown_killswitch's use of net session PnL rather than a sum of losing
trades in isolation.

Once tripped, every order-placement call is blocked for the cooldown's
duration regardless of what triggered the breach -- the call being
evaluated need not itself be the one whose realized P&L crossed the
threshold, since that P&L comes from prior fills, not this call's
arguments.

Entering and exiting the cooldown are reported back via
`RuleOutcome.state_events` as `state_entered`/`state_exited`, one event per
transition, so `PolicyEngine` can write each as its own audit record
distinct from the block records on individual calls -- letting a dashboard
render "cooldown active" as one continuous state rather than reconstructing
it from a stream of per-call blocks. Every `state_entered` is guaranteed a
matching `state_exited`, including on manual `reset()`: reset() cannot emit
an audit record itself (it has no `state`/`now` to attach one to), so it
defers the exit record to the next `check()` call rather than leaving the
span open. Without this, resetting mid-cooldown and immediately re-tripping
on still-fresh loss data would emit a second `state_entered` with no
`state_exited` in between -- an unpaired span the dashboard can't close.

Regulation: SEC Rule 15c3-5(c)(1)(i) (pre-trade controls must include an
automated halt on breach of a defined loss limit) -- the same basis as
drawdown_killswitch; a windowed loss limit is the same control with a
different lookback shape, not a different kind of control.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel

from firewall.pnl_history import PnLHistory
from firewall.rules._util import matches_any
from firewall.rules.base import Rule, RuleConfig, RuleOutcome


class _Params(BaseModel):
    cooldown_loss_threshold: float
    cooldown_loss_window: float = 300
    cooldown_duration_seconds: float = 900
    tool_match: list[str] = ["order"]
    pnl_history_state_key: str = "pnl_history"


class CooldownAfterLossRule(Rule):
    def __init__(self, config: RuleConfig) -> None:
        super().__init__(config)
        self.cfg = _Params.model_validate(config.params)
        self._cooldown_until: float | None = None
        self._reset_exit_pending = False

    def check(
        self, tool_name: str, arguments: dict[str, Any], state: dict[str, Any]
    ) -> RuleOutcome:
        now = state.get("now", time.time())
        state_events: list[tuple[str, str]] = []

        if self._reset_exit_pending:
            state_events.append(
                ("state_exited", "cooldown ended: manually reset")
            )
            self._reset_exit_pending = False

        if self._cooldown_until is not None and now >= self._cooldown_until:
            state_events.append(
                (
                    "state_exited",
                    f"cooldown ended at {now:.0f} after its "
                    f"{self.cfg.cooldown_duration_seconds:.0f}s duration elapsed",
                )
            )
            self._cooldown_until = None

        if self._cooldown_until is None:
            history: PnLHistory | None = state.get(self.cfg.pnl_history_state_key)
            if history is not None:
                recent = history.since(now, self.cfg.cooldown_loss_window)
                net_pnl = sum(e.pnl_usd for e in recent)
                if net_pnl < 0 and -net_pnl > self.cfg.cooldown_loss_threshold:
                    self._cooldown_until = now + self.cfg.cooldown_duration_seconds
                    state_events.append(
                        (
                            "state_entered",
                            f"realized loss ${-net_pnl:,.2f} over the trailing "
                            f"{self.cfg.cooldown_loss_window:.0f}s exceeds cooldown "
                            f"threshold ${self.cfg.cooldown_loss_threshold:,.2f}; "
                            "all order placement blocked for "
                            f"{self.cfg.cooldown_duration_seconds:.0f}s",
                        )
                    )

        if self._cooldown_until is None:
            return RuleOutcome(False, state_events=state_events)

        if not matches_any(tool_name, self.cfg.tool_match):
            return RuleOutcome(False, state_events=state_events)

        # Stable per cooldown episode (doesn't shrink call to call like a
        # "remaining seconds" figure would), so every blocked call in the
        # same episode reports an identical reason -- dedupe/grouping by
        # reason text stays meaningful.
        return RuleOutcome(
            True,
            "cooldown after loss is active (realized loss exceeded "
            f"${self.cfg.cooldown_loss_threshold:,.2f} within "
            f"{self.cfg.cooldown_loss_window:.0f}s); order placement blocked "
            f"until {self._cooldown_until:.0f}",
            state_events=state_events,
        )

    def reset(self) -> None:
        if self._cooldown_until is not None:
            self._reset_exit_pending = True
        self._cooldown_until = None

"""Shared interface for policy rule implementations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class RuleConfig(BaseModel):
    """Common fields every rule declaration in policies/*.yaml has.

    Fields beyond the common ones (`id`, `type`, `enabled`, `severity`,
    `regulation_ref`) are type-specific and captured verbatim in `params`.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    type: str
    enabled: bool = True
    severity: Literal["hard", "soft"]
    regulation_ref: str | None

    @property
    def params(self) -> dict[str, Any]:
        return self.model_extra or {}


@dataclass
class RuleOutcome:
    """Whether a rule fired for a given call, and why.

    `state_events` carries state-machine transitions a stateful rule wants
    recorded as their own audit entries -- e.g. entering/exiting a
    cooldown -- distinct from the block/allow decision on this specific
    call. Each item is `(verdict, reason)`; `PolicyEngine.evaluate` writes
    one audit record per item, tagged with this verdict, whenever an
    audit_writer is configured. Empty for stateless rules and for calls
    that don't cross a state boundary.
    """

    triggered: bool
    reason: str = field(default="")
    state_events: list[tuple[str, str]] = field(default_factory=list)


class Rule:
    """Base class for a rule type. Subclasses implement `check`."""

    def __init__(self, config: RuleConfig) -> None:
        self.id = config.id
        self.type = config.type
        self.enabled = config.enabled
        self.severity = config.severity
        self.regulation_ref = config.regulation_ref
        self.params = config.params

    def check(
        self, tool_name: str, arguments: dict[str, Any], state: dict[str, Any]
    ) -> RuleOutcome:
        raise NotImplementedError

    def reset(self) -> None:
        """Clear any sticky/latched state. No-op for stateless rules."""
        return None

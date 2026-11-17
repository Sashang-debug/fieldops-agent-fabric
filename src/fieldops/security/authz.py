"""Tool authorization decisions for MCP and agent callers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from fieldops.security.principals import (
    SCOPE_CRM_READ,
    SCOPE_TICKET_READ,
    SCOPE_TICKET_WRITE,
    Principal,
)

TOOL_SCOPES: dict[str, str] = {
    "lookup_customer": SCOPE_CRM_READ,
    "list_open_tickets": SCOPE_TICKET_READ,
    "create_ticket": SCOPE_TICKET_WRITE,
}


@dataclass(frozen=True)
class AuthzDecision:
    allowed: bool
    principal_id: str
    tool_name: str
    required_scope: str | None
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PermissionDenied(Exception):
    """Raised when a principal lacks the scope required for a tool."""

    def __init__(self, decision: AuthzDecision):
        self.decision = decision
        super().__init__(decision.reason)


def authorize(principal: Principal, tool_name: str) -> AuthzDecision:
    required_scope = TOOL_SCOPES.get(tool_name)
    if required_scope is None:
        return AuthzDecision(
            allowed=False,
            principal_id=principal.id,
            tool_name=tool_name,
            required_scope=None,
            reason=f"Unknown tool: {tool_name}",
        )
    if required_scope not in principal.scopes:
        return AuthzDecision(
            allowed=False,
            principal_id=principal.id,
            tool_name=tool_name,
            required_scope=required_scope,
            reason=(
                f"Principal {principal.id} lacks required scope "
                f"{required_scope} for tool {tool_name}"
            ),
        )
    return AuthzDecision(
        allowed=True,
        principal_id=principal.id,
        tool_name=tool_name,
        required_scope=required_scope,
        reason=f"Principal {principal.id} authorized for {tool_name}",
    )

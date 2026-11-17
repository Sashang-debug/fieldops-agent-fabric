"""Authorization tests for MCP tool dispatch."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "mcp_servers" / "enterprise_mock"))

from fieldops.security.authz import PermissionDenied, authorize  # noqa: E402
from fieldops.security.principals import UnknownPrincipalError, get_principal  # noqa: E402
from server import dispatch_tool_call  # noqa: E402


_CREATE_ARGS = {
    "customer_id": "cust-1001",
    "title": "Payment outage",
    "priority": "P1",
    "category": "incident",
    "body": "Enterprise customer reports payments down",
}


def test_action_agent_can_create_ticket():
    from fieldops.security.approvals import get_approval_store, reset_approval_store
    from fieldops.tools.enterprise import reset_tickets

    reset_approval_store()
    reset_tickets()

    pending = dispatch_tool_call("create_ticket", _CREATE_ARGS, principal_id="action-agent")
    assert pending["audit"]["allowed"] is True
    assert pending["audit"]["principal_id"] == "action-agent"
    assert pending["audit"]["required_scope"] == "ticket:write"
    assert pending["ok"] is False
    approval_id = pending["data"]["pending_approval_id"]

    get_approval_store().approve(approval_id, "ops-lead")
    payload = dispatch_tool_call(
        "create_ticket",
        {**_CREATE_ARGS, "approval_token": approval_id},
        principal_id="action-agent",
    )
    assert payload["ok"] is True
    assert payload["data"]["ticket_id"].startswith("TKT-")
    assert payload["audit"]["allowed"] is True
    assert payload["audit"]["tool_name"] == "create_ticket"
    assert payload["audit"]["required_scope"] == "ticket:write"


def test_triage_agent_denied_create_ticket_recorded_in_audit():
    payload = dispatch_tool_call("create_ticket", _CREATE_ARGS, principal_id="triage-agent")
    assert payload["ok"] is False
    assert payload["error"]["type"] == "permission_denied"
    assert payload["audit"]["allowed"] is False
    assert payload["audit"]["principal_id"] == "triage-agent"
    assert payload["audit"]["tool_name"] == "create_ticket"
    assert payload["audit"]["required_scope"] == "ticket:write"
    assert "ticket:write" in payload["audit"]["reason"]


def test_unknown_tool_name_rejected():
    principal = get_principal("action-agent")
    decision = authorize(principal, "delete_everything")
    assert decision.allowed is False
    assert decision.required_scope is None
    assert "Unknown tool" in decision.reason

    payload = dispatch_tool_call("delete_everything", {}, principal_id="action-agent")
    assert payload["ok"] is False
    assert payload["error"]["type"] == "permission_denied"
    assert payload["audit"]["allowed"] is False
    assert payload["audit"]["tool_name"] == "delete_everything"


def test_unknown_principal_rejected():
    with pytest.raises(UnknownPrincipalError):
        get_principal("no-such-agent")

    payload = dispatch_tool_call("lookup_customer", {"customer_id": "cust-1001"}, principal_id="no-such-agent")
    assert payload["ok"] is False
    assert payload["error"]["type"] == "unknown_principal"
    assert payload["audit"]["allowed"] is False
    assert payload["audit"]["principal_id"] == "no-such-agent"


def test_permission_denied_exception_carries_decision():
    principal = get_principal("triage-agent")
    decision = authorize(principal, "create_ticket")
    with pytest.raises(PermissionDenied) as exc_info:
        raise PermissionDenied(decision)
    assert exc_info.value.decision.allowed is False
    assert exc_info.value.decision.required_scope == "ticket:write"

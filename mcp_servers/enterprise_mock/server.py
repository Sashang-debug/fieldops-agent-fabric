"""
MCP server exposing mock CRM / ticketing tools.

Run: python mcp_servers/enterprise_mock/server.py

Wire into agents via ADK MCP integration or LangGraph tool wrappers.

Authorization: set FIELDOPS_PRINCIPAL to a principal id from data/principals.json
(default: triage-agent). Tool calls are checked against that principal's scopes.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

# Allow importing fieldops without editable install during dev
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mcp.server import Server  # noqa: E402
from mcp.server.stdio import stdio_server  # noqa: E402
from mcp.types import TextContent, Tool  # noqa: E402

from fieldops.security.authz import AuthzDecision, authorize  # noqa: E402
from fieldops.security.principals import (  # noqa: E402
    UnknownPrincipalError,
    get_principal,
)
from fieldops.tools.enterprise import create_ticket, list_open_tickets, lookup_customer  # noqa: E402

server = Server("enterprise-mock")

DEFAULT_PRINCIPAL_ID = "triage-agent"


def resolve_calling_principal_id() -> str:
    return os.environ.get("FIELDOPS_PRINCIPAL", DEFAULT_PRINCIPAL_ID)


def _error_payload(
    *,
    error_type: str,
    message: str,
    audit: dict[str, Any],
) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {"type": error_type, "message": message},
        "data": {},
        "audit": audit,
    }


def _success_payload(result: Any, decision: AuthzDecision) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "data": result.data,
        "audit": {
            "note": result.audit_note,
            **decision.to_dict(),
        },
    }


def dispatch_tool_call(
    name: str,
    arguments: dict[str, Any],
    principal_id: str | None = None,
) -> dict[str, Any]:
    """Authorize and dispatch a tool call; always returns a structured payload."""
    pid = principal_id if principal_id is not None else resolve_calling_principal_id()
    try:
        principal = get_principal(pid)
    except UnknownPrincipalError:
        return _error_payload(
            error_type="unknown_principal",
            message=f"Unknown principal: {pid}",
            audit={
                "allowed": False,
                "principal_id": pid,
                "tool_name": name,
                "required_scope": None,
                "reason": f"Unknown principal: {pid}",
            },
        )

    decision = authorize(principal, name)
    if not decision.allowed:
        return _error_payload(
            error_type="permission_denied",
            message=decision.reason,
            audit=decision.to_dict(),
        )

    if name == "lookup_customer":
        result = lookup_customer(arguments["customer_id"])
    elif name == "create_ticket":
        result = create_ticket(**arguments)
    elif name == "list_open_tickets":
        result = list_open_tickets(arguments.get("customer_id"))
    else:
        # authorize() already rejects unknown tools; defensive fallback
        return _error_payload(
            error_type="unknown_tool",
            message=f"Unknown tool: {name}",
            audit=decision.to_dict(),
        )

    return _success_payload(result, decision)


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="lookup_customer",
            description="Fetch customer tier and ARR from mock CRM",
            inputSchema={
                "type": "object",
                "properties": {"customer_id": {"type": "string"}},
                "required": ["customer_id"],
            },
        ),
        Tool(
            name="create_ticket",
            description=(
                "Create a ServiceNow-style incident. Requires a prior human approval "
                "token (approval_token) from POST /v1/approvals/{id}; without it, "
                "returns pending_approval_id."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "customer_id": {"type": "string"},
                    "title": {"type": "string"},
                    "priority": {"type": "string"},
                    "category": {"type": "string"},
                    "body": {"type": "string"},
                    "approval_token": {"type": "string"},
                    "requested_by": {"type": "string"},
                },
                "required": ["customer_id", "title", "priority", "category", "body"],
            },
        ),
        Tool(
            name="list_open_tickets",
            description="List open incidents, optionally filtered by customer",
            inputSchema={
                "type": "object",
                "properties": {"customer_id": {"type": "string"}},
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    payload = dispatch_tool_call(name, arguments)
    return [TextContent(type="text", text=json.dumps(payload))]


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

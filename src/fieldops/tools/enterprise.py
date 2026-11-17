"""Mock enterprise tools — stand-in for CRM / ticketing APIs behind customer security perimeters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fieldops.security.approvals import get_approval_store

_TICKETS: dict[str, dict[str, Any]] = {}
_CUSTOMERS: dict[str, dict[str, Any]] = {
    "cust-1001": {"name": "Acme Corp", "tier": "Enterprise", "arr_usd": 240_000},
    "cust-1002": {"name": "Globex", "tier": "Standard", "arr_usd": 48_000},
}


@dataclass
class ToolResult:
    ok: bool
    data: dict[str, Any]
    audit_note: str


def lookup_customer(customer_id: str) -> ToolResult:
    customer = _CUSTOMERS.get(customer_id)
    if not customer:
        return ToolResult(False, {}, f"Customer {customer_id} not found")
    return ToolResult(True, customer, "crm.lookup_customer")


def create_ticket(
    customer_id: str,
    title: str,
    priority: str,
    category: str,
    body: str,
    approval_token: str | None = None,
    requested_by: str = "action-agent",
) -> ToolResult:
    """Create a ticket only when ``approval_token`` references an approved request.

    Without a valid approved token, opens a pending HITL approval and returns
    ``ok=False`` with ``pending_approval_id`` — no ticket is created.
    """
    store = get_approval_store()
    payload = {
        "customer_id": customer_id,
        "title": title,
        "priority": priority,
        "category": category,
        "body": body,
    }

    if approval_token and store.is_valid_approved_token(approval_token, "create_ticket"):
        ticket_id = f"TKT-{len(_TICKETS) + 1001}"
        record = {
            "ticket_id": ticket_id,
            "customer_id": customer_id,
            "title": title,
            "priority": priority,
            "category": category,
            "body": body,
            "status": "open",
            "approval_id": approval_token,
        }
        _TICKETS[ticket_id] = record
        store.consume(approval_token, actor=requested_by)
        return ToolResult(True, record, "servicenow.create_incident")

    approval = store.request("create_ticket", payload, requested_by=requested_by)
    return ToolResult(
        False,
        {
            "pending_approval_id": approval.id,
            "status": "pending_approval",
            "action": "create_ticket",
        },
        "servicenow.create_incident.pending_approval",
    )


def list_open_tickets(customer_id: str | None = None) -> ToolResult:
    tickets = list(_TICKETS.values())
    if customer_id:
        tickets = [t for t in tickets if t["customer_id"] == customer_id]
    return ToolResult(True, {"tickets": tickets}, "servicenow.list_incidents")


def reset_tickets() -> None:
    """Clear in-memory tickets (for tests)."""
    _TICKETS.clear()

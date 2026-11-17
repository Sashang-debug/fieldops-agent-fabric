"""Human-in-the-loop approval gate tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fieldops.api.main import app
from fieldops.security.approvals import ApprovalError, get_approval_store, reset_approval_store
from fieldops.tools.enterprise import create_ticket, list_open_tickets, reset_tickets


_TICKET_ARGS = {
    "customer_id": "cust-1001",
    "title": "Payment outage",
    "priority": "P1",
    "category": "incident",
    "body": "Enterprise customer reports payments down",
}


@pytest.fixture(autouse=True)
def _fresh_store():
    reset_approval_store(ttl_seconds=3600)
    reset_tickets()
    yield
    reset_approval_store(ttl_seconds=3600)
    reset_tickets()


def test_request_approve_create_path():
    pending = create_ticket(**_TICKET_ARGS, requested_by="action-agent")
    assert pending.ok is False
    approval_id = pending.data["pending_approval_id"]
    assert approval_id
    assert list_open_tickets().data["tickets"] == []

    store = get_approval_store()
    listed = store.list_pending()
    assert len(listed) == 1
    assert listed[0].id == approval_id
    assert listed[0].audit_trail[-1]["outcome"] == "requested"

    client = TestClient(app)
    list_resp = client.get("/v1/approvals")
    assert list_resp.status_code == 200
    assert list_resp.json()["count"] == 1
    assert list_resp.json()["approvals"][0]["id"] == approval_id

    decide = client.post(
        f"/v1/approvals/{approval_id}",
        json={"decision": "approve", "approver": "ops-lead"},
    )
    assert decide.status_code == 200
    body = decide.json()["approval"]
    assert body["status"] == "approved"
    assert body["decided_by"] == "ops-lead"
    assert body["audit_trail"][-1] == {
        "approver": "ops-lead",
        "timestamp": body["audit_trail"][-1]["timestamp"],
        "outcome": "approved",
    }

    assert client.get("/v1/approvals").json()["count"] == 0

    created = create_ticket(
        **_TICKET_ARGS,
        approval_token=approval_id,
        requested_by="action-agent",
    )
    assert created.ok is True
    assert created.data["ticket_id"].startswith("TKT-")
    assert created.data["approval_id"] == approval_id
    assert list_open_tickets().data["tickets"][0]["ticket_id"] == created.data["ticket_id"]

    consumed = store.get(approval_id)
    assert consumed is not None
    assert consumed.status == "consumed"
    assert consumed.audit_trail[-1]["outcome"] == "consumed"
    assert consumed.audit_trail[-1]["approver"] == "action-agent"

    # Token cannot be reused
    reuse = create_ticket(
        **_TICKET_ARGS,
        approval_token=approval_id,
        requested_by="action-agent",
    )
    assert reuse.ok is False
    assert "pending_approval_id" in reuse.data


def test_deny_path():
    pending = create_ticket(**_TICKET_ARGS, requested_by="action-agent")
    approval_id = pending.data["pending_approval_id"]

    client = TestClient(app)
    decide = client.post(
        f"/v1/approvals/{approval_id}",
        json={
            "decision": "deny",
            "approver": "ops-lead",
            "reason": "Insufficient evidence",
        },
    )
    assert decide.status_code == 200
    body = decide.json()["approval"]
    assert body["status"] == "denied"
    assert body["reason"] == "Insufficient evidence"
    assert body["audit_trail"][-1]["outcome"] == "denied"
    assert body["audit_trail"][-1]["approver"] == "ops-lead"
    assert body["audit_trail"][-1]["reason"] == "Insufficient evidence"

    blocked = create_ticket(
        **_TICKET_ARGS,
        approval_token=approval_id,
        requested_by="action-agent",
    )
    assert blocked.ok is False
    assert blocked.data["pending_approval_id"] != approval_id
    assert list_open_tickets().data["tickets"] == []


def test_expiry_path():
    clock = {"now": 1_000_000.0}

    def fake_clock() -> float:
        return clock["now"]

    store = reset_approval_store(ttl_seconds=30.0, clock=fake_clock)
    pending = create_ticket(**_TICKET_ARGS, requested_by="action-agent")
    approval_id = pending.data["pending_approval_id"]

    approval = store.get(approval_id)
    assert approval is not None
    assert approval.status == "pending"

    clock["now"] += 31.0
    expired = store.get(approval_id)
    assert expired is not None
    assert expired.status == "expired"
    assert expired.audit_trail[-1]["outcome"] == "expired"
    assert expired.audit_trail[-1]["approver"] == "system"

    assert store.list_pending() == []

    with pytest.raises(ApprovalError):
        store.approve(approval_id, "ops-lead")

    blocked = create_ticket(
        **_TICKET_ARGS,
        approval_token=approval_id,
        requested_by="action-agent",
    )
    assert blocked.ok is False
    assert "pending_approval_id" in blocked.data
    assert list_open_tickets().data["tickets"] == []


def test_action_agent_surfaces_pending_approval_not_as_failure():
    from fieldops.agents.graph import _action_node

    state = {
        "messages": [],
        "request_id": "req-1",
        "customer_id": "cust-1001",
        "user_query": "Enterprise outage affecting payments",
        "category": "incident",
        "rag_context": [],
        "draft_answer": "Escalate to SRE per policy.",
        "reflection_notes": "",
        "tool_results": [],
        "final_response": "",
        "metrics": {},
    }
    out = _action_node(state)  # type: ignore[arg-type]
    assert out.get("pending_approval_id")
    assert "Pending human approval" in out["draft_answer"]
    assert "Ticket created" not in out["draft_answer"]
    ticket_tool = next(t for t in out["tool_results"] if "pending" in t["tool"] or not t.get("ok", True))
    assert ticket_tool["ok"] is False
    assert ticket_tool["result"]["pending_approval_id"] == out["pending_approval_id"]

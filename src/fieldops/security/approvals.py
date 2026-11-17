"""In-memory human-in-the-loop approval store for write operations."""

from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

ApprovalStatus = Literal["pending", "approved", "denied", "expired", "consumed"]
AuditOutcome = Literal["requested", "approved", "denied", "expired", "consumed"]


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


@dataclass
class Approval:
    id: str
    action: str
    payload: dict[str, Any]
    requested_by: str
    status: ApprovalStatus
    created_at: float
    expires_at: float
    decided_by: str | None = None
    decided_at: float | None = None
    reason: str | None = None
    audit_trail: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action,
            "payload": self.payload,
            "requested_by": self.requested_by,
            "status": self.status,
            "created_at": _iso(self.created_at),
            "expires_at": _iso(self.expires_at),
            "decided_by": self.decided_by,
            "decided_at": _iso(self.decided_at) if self.decided_at is not None else None,
            "reason": self.reason,
            "audit_trail": list(self.audit_trail),
        }


class ApprovalError(Exception):
    """Raised when an approval cannot be found or transitioned."""


class ApprovalStore:
    """Thread-safe in-memory store; pending approvals expire after ``ttl_seconds``."""

    def __init__(
        self,
        ttl_seconds: float = 3600.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self._approvals: dict[str, Approval] = {}

    def _now(self) -> float:
        return float(self._clock())

    def _append_audit(
        self,
        approval: Approval,
        *,
        approver: str,
        outcome: AuditOutcome,
        reason: str | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "approver": approver,
            "timestamp": _iso(self._now()),
            "outcome": outcome,
        }
        if reason is not None:
            entry["reason"] = reason
        approval.audit_trail.append(entry)

    def _expire_if_needed(self, approval: Approval) -> Approval:
        if approval.status == "pending" and self._now() >= approval.expires_at:
            approval.status = "expired"
            self._append_audit(
                approval,
                approver="system",
                outcome="expired",
                reason="TTL exceeded",
            )
        return approval

    def request(
        self,
        action: str,
        payload: dict[str, Any],
        requested_by: str,
    ) -> Approval:
        now = self._now()
        approval = Approval(
            id=str(uuid.uuid4()),
            action=action,
            payload=dict(payload),
            requested_by=requested_by,
            status="pending",
            created_at=now,
            expires_at=now + self.ttl_seconds,
        )
        self._append_audit(approval, approver=requested_by, outcome="requested")
        with self._lock:
            self._approvals[approval.id] = approval
        return approval

    def get(self, approval_id: str) -> Approval | None:
        with self._lock:
            approval = self._approvals.get(approval_id)
            if approval is None:
                return None
            return self._expire_if_needed(approval)

    def list_pending(self) -> list[Approval]:
        with self._lock:
            pending: list[Approval] = []
            for approval in self._approvals.values():
                self._expire_if_needed(approval)
                if approval.status == "pending":
                    pending.append(approval)
            return sorted(pending, key=lambda a: a.created_at)

    def approve(self, approval_id: str, approver: str) -> Approval:
        with self._lock:
            approval = self._require(approval_id)
            self._expire_if_needed(approval)
            if approval.status != "pending":
                raise ApprovalError(
                    f"Approval {approval_id} is {approval.status}, cannot approve"
                )
            approval.status = "approved"
            approval.decided_by = approver
            approval.decided_at = self._now()
            self._append_audit(approval, approver=approver, outcome="approved")
            return approval

    def deny(self, approval_id: str, approver: str, reason: str) -> Approval:
        with self._lock:
            approval = self._require(approval_id)
            self._expire_if_needed(approval)
            if approval.status != "pending":
                raise ApprovalError(
                    f"Approval {approval_id} is {approval.status}, cannot deny"
                )
            approval.status = "denied"
            approval.decided_by = approver
            approval.decided_at = self._now()
            approval.reason = reason
            self._append_audit(
                approval, approver=approver, outcome="denied", reason=reason
            )
            return approval

    def consume(self, approval_id: str, actor: str) -> Approval:
        """Mark an approved token as used after a successful write."""
        with self._lock:
            approval = self._require(approval_id)
            self._expire_if_needed(approval)
            if approval.status != "approved":
                raise ApprovalError(
                    f"Approval {approval_id} is {approval.status}, cannot consume"
                )
            approval.status = "consumed"
            self._append_audit(approval, approver=actor, outcome="consumed")
            return approval

    def is_valid_approved_token(self, token: str, action: str) -> bool:
        approval = self.get(token)
        return (
            approval is not None
            and approval.status == "approved"
            and approval.action == action
        )

    def _require(self, approval_id: str) -> Approval:
        approval = self._approvals.get(approval_id)
        if approval is None:
            raise ApprovalError(f"Unknown approval: {approval_id}")
        return approval


_store: ApprovalStore | None = None
_store_lock = threading.Lock()


def get_approval_store() -> ApprovalStore:
    global _store
    with _store_lock:
        if _store is None:
            ttl = float(os.environ.get("APPROVAL_TTL_SECONDS", "3600"))
            _store = ApprovalStore(ttl_seconds=ttl)
        return _store


def reset_approval_store(
    ttl_seconds: float | None = None,
    clock: Callable[[], float] | None = None,
) -> ApprovalStore:
    """Replace the process-wide store (for tests)."""
    global _store
    with _store_lock:
        ttl = (
            float(ttl_seconds)
            if ttl_seconds is not None
            else float(os.environ.get("APPROVAL_TTL_SECONDS", "3600"))
        )
        _store = ApprovalStore(ttl_seconds=ttl, clock=clock)
        return _store

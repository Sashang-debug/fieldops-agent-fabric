"""Security primitives: principals, authorization, and HITL approvals."""

from fieldops.security.approvals import (
    Approval,
    ApprovalError,
    ApprovalStore,
    get_approval_store,
    reset_approval_store,
)
from fieldops.security.authz import AuthzDecision, PermissionDenied, TOOL_SCOPES, authorize
from fieldops.security.principals import (
    SCOPE_CRM_READ,
    SCOPE_TICKET_READ,
    SCOPE_TICKET_WRITE,
    Principal,
    UnknownPrincipalError,
    get_principal,
    load_principals,
)

__all__ = [
    "Approval",
    "ApprovalError",
    "ApprovalStore",
    "AuthzDecision",
    "PermissionDenied",
    "Principal",
    "SCOPE_CRM_READ",
    "SCOPE_TICKET_READ",
    "SCOPE_TICKET_WRITE",
    "TOOL_SCOPES",
    "UnknownPrincipalError",
    "authorize",
    "get_approval_store",
    "get_principal",
    "load_principals",
    "reset_approval_store",
]

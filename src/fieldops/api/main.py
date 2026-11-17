"""FastAPI service — Cloud Run ready entrypoint."""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

from fieldops.agents.finance_graph import run_finance_reconcile
from fieldops.agents.graph import run_support_workflow
from fieldops.config import get_settings
from fieldops.observability.tracing import init_tracing
from fieldops.rag.retriever import KnowledgeRetriever
from fieldops.security.approvals import ApprovalError, get_approval_store

app = FastAPI(
    title="FieldOps Agent Fabric",
    description="Enterprise support + finance reporting QC — multi-agent + RAG + tools + observability",
    version="0.1.0",
)


class SupportRequest(BaseModel):
    query: str = Field(..., examples=["Customer reports us-central1 outage affecting payments"])
    customer_id: str = Field(default="cust-1001", examples=["cust-1001"])


class SupportResponse(BaseModel):
    request_id: str
    category: str | None
    answer: str | None
    metrics: dict
    reflection_notes: str | None = None
    pending_approval_id: str | None = None


class FinanceReconcileRequest(BaseModel):
    document_ref: str = Field(
        ...,
        examples=["finance/q2-board-deck-narrative.md"],
        description="Path relative to the knowledge base root",
    )


class FinanceFinding(BaseModel):
    severity: str
    finding_type: str
    metric: str
    claim: str
    claimed_value: Any = None
    source_value: Any = None
    source_document: str
    confidence: float
    reason: str


class FinanceReconcileResponse(BaseModel):
    request_id: str
    document_ref: str
    findings: list[FinanceFinding]
    report: dict
    metrics: dict
    claims: list[dict] = Field(default_factory=list)


class ApprovalDecisionRequest(BaseModel):
    decision: Literal["approve", "deny"]
    approver: str = Field(..., min_length=1)
    reason: str | None = None


_rag_ready = False


@app.on_event("startup")
def startup() -> None:
    """Keep startup fast — RAG index is baked into the Docker image for Cloud Run."""
    global _rag_ready
    try:
        init_tracing()
    except Exception as exc:
        logger.warning("Tracing init skipped: %s", exc)
    try:
        _rag_ready = KnowledgeRetriever().document_count > 0
    except Exception as exc:
        logger.warning("RAG check failed: %s", exc)


@app.get("/")
def root() -> dict:
    return {
        "service": get_settings().otel_service_name,
        "health": "/health",
        "docs": "/docs",
        "rag_ready": _rag_ready,
    }


@app.get("/health")
@app.get("/healthz")
def healthz() -> dict:
    # Note: Cloud Run reserves /healthz at the edge (returns Google 404). Use /health on Run.
    return {"status": "ok", "service": get_settings().otel_service_name, "rag_ready": _rag_ready}


@app.post("/v1/support", response_model=SupportResponse)
def support(req: SupportRequest) -> SupportResponse:
    result = run_support_workflow(user_query=req.query, customer_id=req.customer_id)
    return SupportResponse(
        request_id=result["request_id"],
        category=result.get("category"),
        answer=result.get("answer"),
        metrics=result.get("metrics", {}),
        reflection_notes=result.get("reflection_notes"),
        pending_approval_id=result.get("pending_approval_id"),
    )


@app.post("/v1/finance/reconcile", response_model=FinanceReconcileResponse)
def finance_reconcile(req: FinanceReconcileRequest) -> FinanceReconcileResponse:
    try:
        result = run_finance_reconcile(document_ref=req.document_ref)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FinanceReconcileResponse(
        request_id=result["request_id"],
        document_ref=result["document_ref"],
        findings=[FinanceFinding(**f) for f in result.get("findings", [])],
        report=result.get("report", {}),
        metrics=result.get("metrics", {}),
        claims=result.get("claims", []),
    )


@app.get("/v1/approvals")
def list_pending_approvals() -> dict[str, Any]:
    store = get_approval_store()
    pending = [a.to_dict() for a in store.list_pending()]
    return {"approvals": pending, "count": len(pending)}


@app.post("/v1/approvals/{approval_id}")
def decide_approval(approval_id: str, body: ApprovalDecisionRequest) -> dict[str, Any]:
    store = get_approval_store()
    try:
        if body.decision == "approve":
            approval = store.approve(approval_id, body.approver)
        else:
            reason = body.reason or "No reason provided"
            approval = store.deny(approval_id, body.approver, reason)
    except ApprovalError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"approval": approval.to_dict()}


def run() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "fieldops.api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=False,
    )


if __name__ == "__main__":
    run()

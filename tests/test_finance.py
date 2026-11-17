"""Finance reporting QC workflow tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fieldops.agents.finance_graph import extract_claims, run_finance_reconcile
from fieldops.api.main import app
from fieldops.rag.ingest import ingest_directory


@pytest.fixture(scope="module", autouse=True)
def _ingest_kb():
    ingest_directory()


def test_extract_planted_arr_and_gm_definition():
    from fieldops.agents.finance_graph import load_document

    text = load_document("finance/q2-board-deck-narrative.md")
    claims = extract_claims(text)
    metrics = {c.metric: c for c in claims}
    assert "ARR" in metrics
    assert metrics["ARR"].claimed_value == 48.2
    assert "Gross Margin" in metrics
    assert metrics["Gross Margin"].definition
    assert "commission" in metrics["Gross Margin"].definition.lower()


def test_reconcile_flags_planted_inconsistencies():
    result = run_finance_reconcile("finance/q2-board-deck-narrative.md")
    types = {f["finding_type"] for f in result["findings"]}
    metrics = {f["metric"] for f in result["findings"]}
    assert "value_mismatch" in types
    assert "definition_mismatch" in types
    assert "ARR" in metrics
    assert "Gross Margin" in metrics
    for f in result["findings"]:
        assert f["severity"] in {"high", "medium", "low", "info"}
        assert "claimed_value" in f
        assert "source_value" in f
        assert f["source_document"]
        assert 0.0 <= f["confidence"] <= 1.0


def test_clean_document_has_zero_findings():
    result = run_finance_reconcile("finance/ops-update-clean.md")
    assert result["findings"] == []


def test_api_finance_reconcile():
    client = TestClient(app)
    resp = client.post(
        "/v1/finance/reconcile",
        json={"document_ref": "finance/q2-board-deck-narrative.md"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["document_ref"] == "finance/q2-board-deck-narrative.md"
    assert len(body["findings"]) >= 2
    finding = body["findings"][0]
    assert {"severity", "claimed_value", "source_value", "source_document", "confidence"} <= set(
        finding
    )


def test_api_finance_reconcile_missing_doc():
    client = TestClient(app)
    resp = client.post(
        "/v1/finance/reconcile",
        json={"document_ref": "finance/does-not-exist.md"},
    )
    assert resp.status_code == 404

"""
Finance reporting QC workflow: extract → retrieve → reconcile → report.

Flags numeric and definitional inconsistencies between a submitted document and
authoritative sources in the knowledge base (forecast tables, KPI glossary).
Reuses fieldops.rag and fieldops.observability — no forked copies.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from fieldops.config import get_settings
from fieldops.observability import RequestMetrics, span
from fieldops.observability.tracing import init_tracing
from fieldops.rag.retriever import KnowledgeRetriever

# Relative tolerance for same-unit numeric compare; rounding edges sit inside this band.
DEFAULT_REL_TOLERANCE = 0.005  # 0.5%
DEFAULT_ABS_TOLERANCE = 0.05

_SOURCE_OF_TRUTH_HINTS = ("forecast", "glossary", "flash-report", "summary")


@dataclass
class Claim:
    claim_id: str
    metric: str
    claimed_value: float | None
    unit: str
    raw_span: str
    definition: str | None = None


@dataclass
class SourceHit:
    metric: str
    value: float | None
    unit: str
    source_document: str
    text: str
    score: float
    definition: str | None = None


@dataclass
class Finding:
    severity: str
    finding_type: str
    metric: str
    claim: str
    claimed_value: float | str | None
    source_value: float | str | None
    source_document: str
    confidence: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FinanceState(TypedDict, total=False):
    request_id: str
    document_ref: str
    document_text: str
    claims: list[dict[str, Any]]
    source_hits: list[dict[str, Any]]
    findings: list[dict[str, Any]]
    report: dict[str, Any]
    metrics: dict[str, Any]


def _kb_root() -> Path:
    return Path(get_settings().knowledge_base_dir)


def load_document(document_ref: str) -> str:
    path = _kb_root() / document_ref
    if not path.is_file():
        raise FileNotFoundError(f"Document not found: {document_ref}")
    return path.read_text(encoding="utf-8")


def _parse_number(raw: str) -> float:
    cleaned = raw.replace(",", "").strip()
    return float(cleaned)


def _normalize_to_canonical(value: float, unit: str) -> tuple[float, str]:
    """Normalize money/percent into comparable canonical units."""
    u = unit.lower()
    if u in {"usd", "dollars", "$"}:
        return value / 1_000_000.0, "usd_millions"
    if u in {"usd_millions", "$m", "millions", "m"}:
        return value, "usd_millions"
    if u in {"percent", "%", "pct"}:
        return value, "percent"
    return value, u or "raw"


def values_within_tolerance(
    claimed: float,
    source: float,
    *,
    rel_tol: float = DEFAULT_REL_TOLERANCE,
    abs_tol: float = DEFAULT_ABS_TOLERANCE,
) -> bool:
    if claimed == source:
        return True
    diff = abs(claimed - source)
    scale = max(abs(claimed), abs(source), 1e-9)
    return diff <= abs_tol or diff / scale <= rel_tol


def _coalesce_groups(match: re.Match[str], *names: str) -> str:
    for name in names:
        value = match.groupdict().get(name)
        if value:
            return value
    return ""


_CLAIM_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "ARR",
        re.compile(
            r"(?P<span>\bARR\b[^\n]{0,48}?"
            r"(?:\$\s*(?P<val_a>\d[\d,]*(?:\.\d+)?)\s*(?P<unit_a>M\b|million|millions)?"
            r"|(?P<val_b>\d[\d,]*(?:\.\d+)?)\s*(?P<unit_b>M\b|million|millions)))",
            re.IGNORECASE,
        ),
        "usd_millions",
    ),
    (
        "NRR",
        re.compile(
            r"(?P<span>\b(?:Net Revenue Retention|NRR)\b[^\n%]{0,60}?"
            r"(?P<val_a>\d[\d,]*(?:\.\d+)?)\s*(?P<unit_a>%))",
            re.IGNORECASE,
        ),
        "percent",
    ),
    (
        "Revenue",
        re.compile(
            r"(?P<span>(?<!Net )\bRevenue\b[^\n]{0,40}?"
            r"(?:\$\s*(?P<val_a>\d[\d,]*(?:\.\d+)?)\s*(?P<unit_a>M\b|million|millions)?"
            r"|(?P<val_b>\d[\d,]*(?:\.\d+)?)\s*(?P<unit_b>M\b|million|millions)))",
            re.IGNORECASE,
        ),
        "usd_millions",
    ),
    (
        "Gross Margin",
        re.compile(
            r"(?P<span>\bGross Margin\b[^\n%]{0,40}?(?P<val_a>\d[\d,]*(?:\.\d+)?)\s*"
            r"(?P<unit_a>%))"
            r"(?:\s*\((?P<defn>[^)]+)\))?",
            re.IGNORECASE,
        ),
        "percent",
    ),
    (
        "OCF",
        re.compile(
            r"(?P<span>\b(?:Operating cash flow|OCF)\b[^\n]{0,40}?"
            r"(?:\$\s*(?P<val_a>\d[\d,]*(?:\.\d+)?)\s*(?P<unit_a>M\b|million|millions)?"
            r"|(?P<val_b>\d[\d,]*(?:\.\d+)?)\s*(?P<unit_b>M\b|million|millions)))",
            re.IGNORECASE,
        ),
        "usd_millions",
    ),
]


def extract_claims(document_text: str) -> list[Claim]:
    # Strip markdown emphasis so **$42.8M** still matches.
    text = document_text.replace("**", "")
    claims: list[Claim] = []
    seen: set[tuple[str, str]] = set()
    for metric, pattern, default_unit in _CLAIM_PATTERNS:
        for match in pattern.finditer(text):
            raw_val = _coalesce_groups(match, "val_a", "val_b")
            if not raw_val or not any(ch.isdigit() for ch in raw_val):
                continue
            span_text = match.group("span").strip()
            unit_token = _coalesce_groups(match, "unit_a", "unit_b")
            key = (metric, raw_val)
            if key in seen:
                continue
            seen.add(key)

            try:
                value = _parse_number(raw_val)
            except ValueError:
                continue

            if default_unit == "usd_millions":
                if not unit_token and value >= 1_000_000:
                    unit = "usd"
                else:
                    unit = "usd_millions"
            elif default_unit == "percent":
                unit = "percent"
            else:
                unit = default_unit

            definition = match.groupdict().get("defn")
            if definition:
                definition = definition.strip()

            claims.append(
                Claim(
                    claim_id=str(uuid.uuid4())[:8],
                    metric=metric,
                    claimed_value=value,
                    unit=unit,
                    raw_span=span_text if not definition else f"{span_text} ({definition})",
                    definition=definition,
                )
            )
    return claims


def _source_priority(source: str) -> int:
    s = source.lower()
    if "glossary" in s:
        return 0
    if "forecast" in s or "summary" in s:
        return 1
    if "flash" in s:
        return 2
    return 5


def _parse_source_value(metric: str, text: str) -> tuple[float | None, str]:
    """Pull authoritative numeric for a metric from a retrieved chunk."""
    if metric == "ARR":
        m = re.search(
            r"ARR\s*\(\$M\)\s*\|\s*(?P<val>[\d.]+)",
            text,
            re.IGNORECASE,
        ) or re.search(
            r"ARR(?:\s+Actual(?:\s+of)?)?\s*(?:of\s+|at\s+|ended Q\d at\s+|:\s*)?\$?(?P<val>[\d.]+)\s*M?",
            text,
            re.IGNORECASE,
        )
        if m:
            return _parse_number(m.group("val")), "usd_millions"
    if metric == "Gross Margin":
        m = re.search(
            r"Gross Margin\s*\(%\)\s*\|\s*(?P<val>[\d.]+)",
            text,
            re.IGNORECASE,
        ) or re.search(
            r"Gross Margin(?:\s+Actual(?:\s+of)?)?\s*(?:of\s+|was\s+|:\s*)?(?P<val>[\d.]+)\s*%?",
            text,
            re.IGNORECASE,
        )
        if m:
            return _parse_number(m.group("val")), "percent"
    if metric == "NRR":
        m = re.search(
            r"NRR\s*\(%\)\s*\|\s*(?P<val>[\d.]+)",
            text,
            re.IGNORECASE,
        ) or re.search(
            r"NRR(?:\s+of)?\s*(?P<val>[\d.]+)\s*%?",
            text,
            re.IGNORECASE,
        )
        if m:
            return _parse_number(m.group("val")), "percent"
    if metric == "Revenue":
        m = re.search(
            r"Revenue\s*\(\$M\)\s*\|\s*(?P<val>[\d.]+)",
            text,
            re.IGNORECASE,
        ) or re.search(
            r"Revenue(?:\s+Actual)?\s*(?:of\s+|:\s*)?\$?(?P<val>[\d.]+)\s*M?",
            text,
            re.IGNORECASE,
        )
        if m:
            return _parse_number(m.group("val")), "usd_millions"
    if metric == "OCF":
        m = re.search(
            r"OCF\s*\(\$M\)\s*\|\s*(?P<val>[\d.]+)",
            text,
            re.IGNORECASE,
        ) or re.search(
            r"(?:OCF|Operating cash flow)(?:\s+was)?\s*\$?(?P<val>[\d.]+)\s*M?",
            text,
            re.IGNORECASE,
        )
        if m:
            return _parse_number(m.group("val")), "usd_millions"
    return None, "raw"


def _parse_glossary_definition(metric: str, text: str) -> str | None:
    if "glossary" not in text.lower() and "##" not in text:
        # Still allow glossary chunks that lead with the metric heading.
        pass
    if metric == "Gross Margin":
        m = re.search(
            r"##\s*Gross Margin\s*(?P<body>.+?)(?=\n##|\Z)",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if m:
            return " ".join(m.group("body").split())
        if "does **not** include sales commissions" in text.lower() or "does not include sales commissions" in text.lower():
            return text.strip()
    return None


def _definition_conflicts(claim_defn: str, glossary_defn: str) -> bool:
    claim_l = claim_defn.lower()
    gloss_l = glossary_defn.lower()

    # Aligned with glossary: commissions explicitly excluded from margin.
    if "commission" in claim_l and any(
        token in claim_l for token in ("exclud", "only", "not include", "excluding")
    ):
        return False

    # Conflict: claim folds commissions into Gross Margin.
    claim_includes_commissions = (
        "and sales commissions" in claim_l
        or "including commission" in claim_l
        or ("less cogs and" in claim_l and "commission" in claim_l)
        or ("cogs and sales commissions" in claim_l)
    )
    gloss_excludes_commissions = (
        "not include sales commissions" in gloss_l
        or "does **not** include sales commissions" in gloss_l
        or ("commissions" in gloss_l and "not" in gloss_l)
    )
    return claim_includes_commissions and gloss_excludes_commissions


def retrieve_sources(
    claims: list[Claim],
    document_ref: str,
    retriever: KnowledgeRetriever | None = None,
) -> list[SourceHit]:
    retriever = retriever or KnowledgeRetriever()
    hits: list[SourceHit] = []
    for claim in claims:
        queries = [
            f"{claim.metric} Actual forecast",
            f"{claim.metric} glossary definition",
            f"{claim.metric} {claim.claimed_value}",
        ]
        chunks: list[dict[str, Any]] = []
        for q in queries:
            chunks.extend(retriever.search(q, k=6))

        # De-dupe by chunk id; drop the document under review as a truth source.
        seen_ids: set[str] = set()
        filtered: list[dict[str, Any]] = []
        for chunk in chunks:
            cid = chunk["id"]
            source = str((chunk.get("metadata") or {}).get("source", ""))
            if cid in seen_ids:
                continue
            if source.replace("\\", "/") == document_ref.replace("\\", "/"):
                continue
            seen_ids.add(cid)
            filtered.append(chunk)

        filtered.sort(
            key=lambda c: (
                _source_priority(str((c.get("metadata") or {}).get("source", ""))),
                -float(c.get("score") or 0),
            )
        )

        for chunk in filtered[:8]:
            source = str((chunk.get("metadata") or {}).get("source", chunk["id"]))
            text = chunk.get("text") or ""
            value, unit = _parse_source_value(claim.metric, text)
            definition = _parse_glossary_definition(claim.metric, text)
            if value is None and definition is None:
                continue
            hits.append(
                SourceHit(
                    metric=claim.metric,
                    value=value,
                    unit=unit,
                    source_document=source,
                    text=text[:500],
                    score=float(chunk.get("score") or 0),
                    definition=definition,
                )
            )
    return hits


def reconcile_claims(
    claims: list[Claim],
    source_hits: list[SourceHit],
    *,
    rel_tol: float = DEFAULT_REL_TOLERANCE,
    abs_tol: float = DEFAULT_ABS_TOLERANCE,
) -> list[Finding]:
    findings: list[Finding] = []
    for claim in claims:
        metric_hits = [h for h in source_hits if h.metric == claim.metric]
        numeric_hits = [h for h in metric_hits if h.value is not None]
        def_hits = [h for h in metric_hits if h.definition]

        # Prefer forecast/summary for numbers; glossary for definitions.
        numeric_hits.sort(key=lambda h: _source_priority(h.source_document))
        def_hits.sort(key=lambda h: _source_priority(h.source_document))

        if claim.definition and def_hits:
            gloss = def_hits[0]
            if gloss.definition and _definition_conflicts(claim.definition, gloss.definition):
                findings.append(
                    Finding(
                        severity="high",
                        finding_type="definition_mismatch",
                        metric=claim.metric,
                        claim=claim.raw_span,
                        claimed_value=claim.definition,
                        source_value=gloss.definition[:240],
                        source_document=gloss.source_document,
                        confidence=0.92,
                        reason=(
                            f"{claim.metric} definition in the document conflicts with "
                            f"the glossary ({gloss.source_document})."
                        ),
                    )
                )

        if claim.claimed_value is None or not numeric_hits:
            continue

        claimed_n, claimed_u = _normalize_to_canonical(claim.claimed_value, claim.unit)

        def _hit_distance(hit: SourceHit) -> float:
            assert hit.value is not None
            source_n, source_u = _normalize_to_canonical(hit.value, hit.unit)
            if source_u != claimed_u:
                return 1e12
            return abs(source_n - claimed_n)

        numeric_hits = sorted(
            numeric_hits,
            key=lambda h: (_hit_distance(h), _source_priority(h.source_document)),
        )

        matched: SourceHit | None = None
        residual: list[tuple[SourceHit, float, str]] = []
        for hit in numeric_hits:
            assert hit.value is not None
            source_n, source_u = _normalize_to_canonical(hit.value, hit.unit)
            if claimed_u != source_u:
                residual.append((hit, source_n, source_u))
                continue
            if values_within_tolerance(claimed_n, source_n, rel_tol=rel_tol, abs_tol=abs_tol):
                matched = hit
                break
            residual.append((hit, source_n, source_u))

        if matched:
            continue

        if not residual:
            continue

        # Prefer same-unit residuals for the mismatch finding.
        same_unit = [(h, sn, su) for h, sn, su in residual if su == claimed_u]
        if same_unit:
            same_unit.sort(key=lambda item: _source_priority(item[0].source_document))
            best, source_n, source_u = same_unit[0]
            rel = abs(claimed_n - source_n) / max(abs(source_n), 1e-9)
            severity = "high" if rel >= 0.05 else "medium"
            findings.append(
                Finding(
                    severity=severity,
                    finding_type="value_mismatch",
                    metric=claim.metric,
                    claim=claim.raw_span,
                    claimed_value=claim.claimed_value,
                    source_value=best.value,
                    source_document=best.source_document,
                    confidence=min(0.99, 0.75 + float(best.score) * 0.2),
                    reason=(
                        f"{claim.metric} claimed {claim.claimed_value} ({claim.unit}) vs "
                        f"source {best.value} ({best.unit}) in {best.source_document}."
                    ),
                )
            )
        else:
            best, source_n, source_u = residual[0]
            findings.append(
                Finding(
                    severity="medium",
                    finding_type="unit_mismatch",
                    metric=claim.metric,
                    claim=claim.raw_span,
                    claimed_value=claim.claimed_value,
                    source_value=best.value,
                    source_document=best.source_document,
                    confidence=0.7,
                    reason=f"Unit mismatch after normalization ({claimed_u} vs {source_u}).",
                )
            )
    return findings


def _extract_node(state: FinanceState) -> dict[str, Any]:
    with span("finance.extract", {"request_id": state["request_id"]}):
        text = state.get("document_text") or load_document(state["document_ref"])
        claims = extract_claims(text)
    return {
        "document_text": text,
        "claims": [asdict(c) for c in claims],
        "metrics": {
            **dict(state.get("metrics") or {}),
            "claims_extracted": len(claims),
        },
    }


def _retrieve_node(state: FinanceState) -> dict[str, Any]:
    with span("finance.retrieve", {"request_id": state["request_id"]}):
        claims = [Claim(**c) for c in state.get("claims") or []]
        hits = retrieve_sources(claims, state["document_ref"])
    metrics = dict(state.get("metrics") or {})
    metrics["rag_chunks_retrieved"] = len(hits)
    return {
        "source_hits": [asdict(h) for h in hits],
        "metrics": metrics,
    }


def _reconcile_node(state: FinanceState) -> dict[str, Any]:
    with span("finance.reconcile", {"request_id": state["request_id"]}):
        claims = [Claim(**c) for c in state.get("claims") or []]
        hits = [SourceHit(**h) for h in state.get("source_hits") or []]
        findings = reconcile_claims(claims, hits)
    return {
        "findings": [f.to_dict() for f in findings],
        "metrics": {
            **dict(state.get("metrics") or {}),
            "findings_count": len(findings),
        },
    }


def _report_node(state: FinanceState) -> dict[str, Any]:
    with span("finance.report", {"request_id": state["request_id"]}):
        findings = list(state.get("findings") or [])
        by_severity: dict[str, int] = {}
        for f in findings:
            by_severity[f["severity"]] = by_severity.get(f["severity"], 0) + 1
        report = {
            "request_id": state["request_id"],
            "document_ref": state["document_ref"],
            "claims_reviewed": len(state.get("claims") or []),
            "findings_count": len(findings),
            "findings_by_severity": by_severity,
            "summary": (
                "No inconsistencies detected."
                if not findings
                else f"Detected {len(findings)} inconsistenc{'y' if len(findings) == 1 else 'ies'}."
            ),
        }
    return {"report": report}


def build_finance_graph() -> Any:
    graph = StateGraph(FinanceState)
    graph.add_node("extract", _extract_node)
    graph.add_node("retrieve", _retrieve_node)
    graph.add_node("reconcile", _reconcile_node)
    graph.add_node("report", _report_node)
    graph.set_entry_point("extract")
    graph.add_edge("extract", "retrieve")
    graph.add_edge("retrieve", "reconcile")
    graph.add_edge("reconcile", "report")
    graph.add_edge("report", END)
    return graph.compile()


def run_finance_reconcile(
    document_ref: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    init_tracing()
    rid = request_id or str(uuid.uuid4())
    app = build_finance_graph()
    initial: FinanceState = {
        "request_id": rid,
        "document_ref": document_ref,
        "claims": [],
        "source_hits": [],
        "findings": [],
        "report": {},
        "metrics": {},
    }
    with span("workflow.finance_reconcile", {"request_id": rid, "document_ref": document_ref}):
        result = app.invoke(initial)

    metrics = RequestMetrics(request_id=rid)
    metrics.rag_chunks_retrieved = int(result.get("metrics", {}).get("rag_chunks_retrieved", 0))
    out_metrics = metrics.to_dict()
    out_metrics["claims_extracted"] = int(result.get("metrics", {}).get("claims_extracted", 0))
    out_metrics["findings_count"] = int(result.get("metrics", {}).get("findings_count", 0))

    return {
        "request_id": rid,
        "document_ref": document_ref,
        "claims": result.get("claims", []),
        "findings": result.get("findings", []),
        "report": result.get("report", {}),
        "metrics": out_metrics,
    }

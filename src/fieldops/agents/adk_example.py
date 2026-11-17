"""
Optional ADK variant — same support workflow via Google's Agent Development Kit.

Enable after: pip install google-adk && gcloud auth application-default login

Optional at import time so the default LangGraph path runs without ADK credentials.
"""

from __future__ import annotations


def build_adk_support_agent():
    """
    Skeleton for an ADK-based agent with MCP tool wiring.

    Extend once `google-adk` is configured for your GCP project.
    """
    try:
        from google.adk.agents import LlmAgent  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError("Install google-adk to use the ADK code path") from exc

    return LlmAgent(
        name="fieldops_support",
        model="gemini-2.0-flash",
        instruction=(
            "You are an enterprise support copilot. Triage requests, cite policy, "
            "and propose ticket actions. Use tools for CRM lookup and incident creation."
        ),
    )

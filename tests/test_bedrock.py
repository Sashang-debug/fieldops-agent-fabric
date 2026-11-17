"""Unit tests for the AWS Bedrock LLM provider."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from fieldops.config import Settings, get_settings
from fieldops.llm import BedrockChatModel, get_chat_model
from fieldops.observability.metrics import RequestMetrics


@pytest.fixture
def bedrock_settings() -> Settings:
    return Settings(
        llm_provider="bedrock",
        aws_region="us-west-2",
        bedrock_model_id="anthropic.claude-3-5-haiku-20241022-v1:0",
        cost_per_1m_input_tokens=0.25,
        cost_per_1m_output_tokens=1.25,
    )


@pytest.fixture
def converse_response() -> dict:
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": '{"category":"incident","answer":"Escalate to SRE"}'}],
            }
        },
        "usage": {
            "inputTokens": 120,
            "outputTokens": 40,
            "totalTokens": 160,
        },
        "stopReason": "end_turn",
    }


def test_bedrock_converse_request_shaping(bedrock_settings: Settings, converse_response: dict):
    client = MagicMock()
    client.converse.return_value = converse_response

    model = BedrockChatModel(bedrock_settings, client=client)
    result = model.invoke("You are a triage agent.", "Customer reports an outage")

    client.converse.assert_called_once_with(
        modelId="anthropic.claude-3-5-haiku-20241022-v1:0",
        system=[{"text": "You are a triage agent."}],
        messages=[
            {
                "role": "user",
                "content": [{"text": "Customer reports an outage"}],
            }
        ],
        inferenceConfig={"temperature": 0.2},
    )
    assert result.content == '{"category":"incident","answer":"Escalate to SRE"}'
    assert result.usage.input_tokens == 120
    assert result.usage.output_tokens == 40
    assert result.usage.total_tokens == 160
    assert result.usage.model == bedrock_settings.bedrock_model_id
    assert result.usage.latency_ms >= 0
    assert result.raw == converse_response


def test_bedrock_usage_flows_through_request_metrics(
    bedrock_settings: Settings, converse_response: dict
):
    client = MagicMock()
    client.converse.return_value = converse_response
    model = BedrockChatModel(bedrock_settings, client=client)

    metrics = RequestMetrics(request_id="req-bedrock-1")
    response = model.invoke("system", "user")
    metrics.add_usage(response.usage)

    assert metrics.total_input_tokens == 120
    assert metrics.total_output_tokens == 40
    assert metrics.total_tokens == 160

    # (120/1e6)*0.25 + (40/1e6)*1.25 = 0.00003 + 0.00005 = 0.00008
    expected_cost = response.usage.estimated_cost_usd(bedrock_settings)
    assert expected_cost == 0.00008

    with patch("fieldops.config.get_settings", return_value=bedrock_settings):
        payload = metrics.to_dict()
    assert payload["total_tokens"] == 160
    assert payload["estimated_cost_usd"] == expected_cost
    assert payload["llm_calls"] == 1
    assert payload["request_id"] == "req-bedrock-1"


def test_get_chat_model_bedrock_uses_boto3_client(bedrock_settings: Settings, converse_response: dict):
    mock_client = MagicMock()
    mock_client.converse.return_value = converse_response
    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = mock_client

    with patch.dict("sys.modules", {"boto3": mock_boto3}):
        model = get_chat_model(bedrock_settings)
        assert isinstance(model, BedrockChatModel)
        result = model.invoke("sys", "hello")

    mock_boto3.client.assert_called_once_with("bedrock-runtime", region_name="us-west-2")
    assert result.usage.input_tokens == 120
    assert "incident" in result.content


def test_get_chat_model_bedrock_requires_model_id():
    settings = Settings(llm_provider="bedrock", bedrock_model_id="")
    with pytest.raises(ValueError, match="BEDROCK_MODEL_ID"):
        get_chat_model(settings)


def test_bedrock_settings_from_env(monkeypatch: pytest.MonkeyPatch):
    get_settings.cache_clear()
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0")
    try:
        settings = Settings()
        assert settings.llm_provider == "bedrock"
        assert settings.aws_region == "eu-west-1"
        assert settings.bedrock_model_id == "amazon.nova-lite-v1:0"
    finally:
        get_settings.cache_clear()

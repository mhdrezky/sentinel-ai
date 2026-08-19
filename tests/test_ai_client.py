"""AI client — response parsing and failure modes.

Small models are unreliable formatters, so the parser is tested harder than
the happy path deserves.
"""

from __future__ import annotations

import json

import pytest

from sentinel_ai.ai.client import AIClient, AIUnavailable, parse_verdict
from sentinel_ai.config import AIConfig
from sentinel_ai.models import Ecosystem, PackageChange, Severity

VALID_VERDICT = {
    "risk_level": "high",
    "confidence": 0.85,
    "summary": "The package fetches a remote script during install.",
    "indicators": ["postinstall downloads a remote payload"],
    "recommended_action": "Remove the dependency.",
    "packages": [{"name": "evil-pkg", "risk_level": "high", "reason": "downloads code"}],
}


class TestParseVerdict:
    def test_clean_json(self):
        verdict = parse_verdict(json.dumps(VALID_VERDICT))
        assert verdict.risk_level is Severity.HIGH
        assert verdict.confidence == pytest.approx(0.85)
        assert verdict.packages[0].name == "evil-pkg"

    def test_markdown_fenced_json(self):
        content = f"```json\n{json.dumps(VALID_VERDICT)}\n```"
        assert parse_verdict(content).risk_level is Severity.HIGH

    def test_json_wrapped_in_prose(self):
        content = (
            "Sure, here is my analysis:\n"
            f"{json.dumps(VALID_VERDICT)}\n"
            "Let me know if you need more detail."
        )
        assert parse_verdict(content).risk_level is Severity.HIGH

    def test_braces_inside_strings_do_not_break_extraction(self):
        payload = {
            "risk_level": "low",
            "confidence": 0.5,
            "summary": 'script contains "${HOME}" and a } brace',
            "indicators": [],
            "recommended_action": "",
            "packages": [],
        }
        assert parse_verdict(json.dumps(payload)).summary.endswith("} brace")

    def test_unknown_severity_falls_back_to_medium(self):
        payload = dict(VALID_VERDICT, risk_level="spicy")
        assert parse_verdict(json.dumps(payload)).risk_level is Severity.MEDIUM

    def test_confidence_is_clamped(self):
        assert (
            parse_verdict(json.dumps(dict(VALID_VERDICT, confidence=5))).confidence == 1.0
        )
        assert (
            parse_verdict(json.dumps(dict(VALID_VERDICT, confidence=-2))).confidence
            == 0.0
        )
        assert (
            parse_verdict(json.dumps(dict(VALID_VERDICT, confidence="n/a"))).confidence
            == 0.0
        )

    def test_missing_fields_use_defaults(self):
        verdict = parse_verdict('{"risk_level": "none"}')
        assert verdict.risk_level is Severity.NONE
        assert verdict.packages == []

    def test_reasoning_field_is_used_when_content_empty(self):
        payload = dict(VALID_VERDICT, summary="from reasoning field")
        content = json.dumps(payload)
        assert parse_verdict(content).summary == "from reasoning field"

    def test_reasoning_field_via_message_content(self):
        from sentinel_ai.ai.json_utils import message_content

        message = {"content": "", "reasoning": json.dumps(VALID_VERDICT)}
        assert parse_verdict(message_content(message)).risk_level is Severity.HIGH

    def test_qwen_think_tags_are_stripped(self):
        think_open, think_close = "<" + "think>", "</" + "think>"
        content = (
            f"{think_open}Let me review this carefully.{think_close}\n"
            f"{json.dumps(VALID_VERDICT)}"
        )
        assert parse_verdict(content).risk_level is Severity.HIGH

    def test_doubled_opening_brace_from_vllm(self):
        payload = json.dumps(VALID_VERDICT)
        assert parse_verdict("{{" + payload[1:]).risk_level is Severity.HIGH

    def test_error_includes_response_preview(self):
        with pytest.raises(AIUnavailable, match="got: 'plain text only'"):
            parse_verdict("plain text only")

    @pytest.mark.parametrize("content", ["", "I cannot help with that.", "[1, 2, 3]"])
    def test_unusable_responses_raise(self, content):
        with pytest.raises(AIUnavailable):
            parse_verdict(content)


class TestClientTransport:
    def _config(self) -> AIConfig:
        return AIConfig(base_url="http://model.local/v1", model="test-model")

    def _changes(self) -> list[PackageChange]:
        return [
            PackageChange(
                name="evil-pkg",
                ecosystem=Ecosystem.NPM,
                new_version="1.0.0",
                manifest_path="package.json",
            )
        ]

    def test_successful_call(self, httpx_mock):
        httpx_mock.add_response(
            url="http://model.local/v1/chat/completions",
            json={"choices": [{"message": {"content": json.dumps(VALID_VERDICT)}}]},
        )
        verdict = AIClient(self._config()).analyse(self._changes(), [], {})
        assert verdict.risk_level is Severity.HIGH

    def test_no_changes_skips_the_network_entirely(self):
        # No httpx_mock registration: a request here would fail the test.
        assert AIClient(self._config()).analyse([], [], {}).risk_level is Severity.NONE

    def test_server_error_raises_ai_unavailable(self, httpx_mock):
        httpx_mock.add_response(status_code=500, text="internal error")
        with pytest.raises(AIUnavailable, match="HTTP 500"):
            AIClient(self._config()).analyse(self._changes(), [], {})

    def test_connection_failure_raises_ai_unavailable(self, httpx_mock):
        import httpx

        httpx_mock.add_exception(httpx.ConnectError("refused"))
        with pytest.raises(AIUnavailable, match="could not reach"):
            AIClient(self._config()).analyse(self._changes(), [], {})

    def test_unexpected_response_shape_raises(self, httpx_mock):
        httpx_mock.add_response(json={"unexpected": True})
        with pytest.raises(AIUnavailable, match="unexpected response shape"):
            AIClient(self._config()).analyse(self._changes(), [], {})

    def test_api_key_is_sent_when_configured(self, httpx_mock):
        httpx_mock.add_response(
            json={"choices": [{"message": {"content": '{"risk_level":"none"}'}}]}
        )
        config = self._config()
        config.api_key = "secret-token"
        AIClient(config).analyse(self._changes(), [], {})
        request = httpx_mock.get_requests()[0]
        assert request.headers["Authorization"] == "Bearer secret-token"

    def test_qwen_thinking_is_disabled_by_default(self, httpx_mock):
        httpx_mock.add_response(
            json={"choices": [{"message": {"content": '{"risk_level":"none"}'}}]}
        )
        AIClient(self._config()).analyse(self._changes(), [], {})
        payload = json.loads(httpx_mock.get_requests()[0].content)
        # Top level, not under `extra_body`: vLLM reads the raw body and silently
        # ignores that wrapper, so the model kept thinking and spent the budget.
        assert payload["chat_template_kwargs"] == {"enable_thinking": False}
        assert "extra_body" not in payload


class TestPromptSafety:
    def test_evidence_is_marked_as_untrusted_data(self):
        from sentinel_ai.ai.prompts import SYSTEM_PROMPT, build_user_prompt

        prompt = build_user_prompt(
            [
                PackageChange(
                    name="pkg",
                    ecosystem=Ecosystem.NPM,
                    new_version="1.0.0",
                    manifest_path="package.json",
                )
            ],
            [],
            {},
        )
        assert "<evidence>" in prompt and "</evidence>" in prompt
        # The system prompt must tell the model that evidence is not instruction.
        assert "untrusted data" in SYSTEM_PROMPT

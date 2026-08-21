"""Salvaging JSON from a small model's reply.

Small models are unreliable formatters, so this is tested harder than the happy
path deserves. The diff reviewer is the only caller now, and it asks for a tiny
object under a tight token budget — which is exactly the situation where a
model wraps its answer in prose, a fence, or a thinking block.
"""

from __future__ import annotations

import json

import pytest

from sentinel_ai.ai.json_utils import (
    extract_json_object,
    message_content,
    prepare_model_content,
)

PAYLOAD = {"v": "notice", "f": [{"c": "network", "s": "high"}]}


class TestExtractJsonObject:
    def test_clean_json(self):
        raw = json.dumps(PAYLOAD)
        assert json.loads(extract_json_object(raw)) == PAYLOAD

    def test_markdown_fence_is_stripped(self):
        raw = f"```json\n{json.dumps(PAYLOAD)}\n```"
        assert json.loads(extract_json_object(raw)) == PAYLOAD

    def test_surrounding_prose_is_discarded(self):
        raw = f"Here is the result: {json.dumps(PAYLOAD)}. Hope that helps!"
        assert json.loads(extract_json_object(raw)) == PAYLOAD

    def test_braces_inside_strings_do_not_end_the_object(self):
        payload = {"v": "pass", "f": [], "note": 'a } and a { inside "quotes"'}
        raw = f"prose {json.dumps(payload)} more prose"
        assert json.loads(extract_json_object(raw)) == payload

    def test_doubled_opening_brace_from_vllm(self):
        """`{{` cannot start valid JSON, so one brace is dropped before parsing."""
        raw = json.dumps(PAYLOAD)
        assert json.loads(extract_json_object("{{" + raw[1:])) == PAYLOAD

    @pytest.mark.parametrize("content", ["", "I cannot help with that.", "[1, 2, 3]"])
    def test_returns_none_when_there_is_no_object(self, content):
        assert extract_json_object(content) is None


class TestPrepareModelContent:
    def test_think_tags_are_stripped(self):
        think_open, think_close = "<" + "think>", "</" + "think>"
        raw = f"{think_open}weighing it up{think_close}{json.dumps(PAYLOAD)}"
        assert json.loads(extract_json_object(prepare_model_content(raw))) == PAYLOAD

    def test_content_without_think_tags_is_unchanged(self):
        assert prepare_model_content("  plain  ") == "plain"


class TestMessageContent:
    def test_plain_content(self):
        assert message_content({"content": "hello"}) == "hello"

    def test_structured_content_parts_are_joined(self):
        message = {
            "content": [
                {"type": "text", "text": "one "},
                {"type": "image", "url": "ignored"},
                {"type": "text", "text": "two"},
            ]
        }
        assert message_content(message) == "one two"

    @pytest.mark.parametrize("key", ["reasoning_content", "reasoning"])
    def test_falls_back_to_reasoning_when_content_is_empty(self, key):
        """vLLM + Qwen3 in thinking mode leaves `content` empty."""
        assert message_content({"content": "", key: "the answer"}) == "the answer"

    def test_non_dict_is_empty(self):
        assert message_content("not a message") == ""

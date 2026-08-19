"""Coaxing JSON out of a small model's reply.

Both the dependency reviewer and the diff reviewer talk to the same on-prem
server, and both have to cope with the same formatting habits: markdown fences,
a sentence wrapped around the object, Qwen3 thinking blocks, and answers that
land in `reasoning` instead of `content`. The helpers live here so the two
clients share one hardening story rather than drifting apart.
"""

from __future__ import annotations

import re

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_THINK_RE = re.compile(r"<\s*think\s*>.*?<\s*/\s*think\s*>", re.DOTALL | re.IGNORECASE)


def message_content(message: object) -> str:
    """Normalise OpenAI-compatible message shapes into plain text."""
    if not isinstance(message, dict):
        return ""
    content = message.get("content") or ""
    if isinstance(content, list):
        content = "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    text = str(content).strip()
    if text:
        return text
    # vLLM + Qwen3 may put the answer in `reasoning` or `reasoning_content`
    # when thinking mode is active, leaving `content` empty.
    for key in ("reasoning_content", "reasoning"):
        fallback = str(message.get(key) or "").strip()
        if fallback:
            return fallback
    return ""


def prepare_model_content(content: str) -> str:
    """Drop Qwen thinking blocks and other wrapper noise before JSON extraction."""
    text = content.strip()
    while True:
        stripped = _THINK_RE.sub("", text).strip()
        if stripped == text:
            break
        text = stripped
    return text


def extract_json_object(content: str) -> str | None:
    if not content:
        return None
    text = content.strip()

    # Strip markdown fences (```json ... ``` or ``` ... ```)
    if fenced := _FENCE_RE.search(text):
        text = fenced.group(1).strip()

    # vLLM + Qwen3.6 occasionally emits a doubled opening brace. `{{` can
    # never start valid JSON, so dropping one is safe and repairs both the
    # `{{...}` and `{{...}}` shapes before the walk below sees them.
    if text.startswith("{{") and not text.startswith("{{{"):
        text = text[1:]

    # Strip any remaining prose: only keep the first complete JSON object.
    # Some LLMs wrap the response in sentences like "The answer is { ... }."
    return _extract_balanced_object(text)


def _extract_balanced_object(text: str) -> str | None:
    start = text.find("{")
    if start == -1:
        return None

    # Walk to the matching close brace so trailing prose is discarded.
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None

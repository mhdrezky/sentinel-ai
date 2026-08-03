"""HTTP client for the on-prem QWEN server.

Targets the OpenAI-compatible `/chat/completions` shape, which vLLM, Ollama,
llama.cpp and TGI all expose. Only `base_url` and `model` should need changing
to point at a different deployment.

The client is deliberately synchronous and single-shot: a pre-commit hook makes
one call, and async plumbing would only add startup cost to the binary.
"""

from __future__ import annotations

import json
import re

import httpx
from pydantic import ValidationError

from ..config import AIConfig
from ..manifests import ParsedManifest
from ..models import AIVerdict, Finding, PackageChange, Severity
from .prompts import SYSTEM_PROMPT, build_user_prompt


class AIUnavailable(RuntimeError):
    """The server could not be reached, timed out, or returned an error.

    Whether this blocks the commit is `AIConfig.fail_open`'s decision, not
    the client's.
    """


class QwenClient:
    def __init__(self, config: AIConfig) -> None:
        self.config = config

    def analyse(
        self,
        changes: list[PackageChange],
        findings: list[Finding],
        manifests: dict[str, ParsedManifest],
    ) -> AIVerdict:
        """Ask the model for a verdict on the changed packages.

        Raises `AIUnavailable` on any transport or protocol failure.
        """
        if not changes:
            return AIVerdict()

        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": build_user_prompt(changes, findings, manifests),
                },
            ],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_output_tokens,
            "stream": False,
            # Honoured by vLLM and Ollama; servers that do not know it ignore it.
            "response_format": {"type": "json_object"},
        }

        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        url = f"{self.config.base_url.rstrip('/')}/chat/completions"

        try:
            with httpx.Client(timeout=self.config.timeout_seconds) as client:
                response = client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise AIUnavailable(
                f"QWEN server did not respond within "
                f"{self.config.timeout_seconds:.0f}s ({url})"
            ) from exc
        except httpx.HTTPError as exc:
            raise AIUnavailable(f"could not reach QWEN server at {url}: {exc}") from exc

        if response.status_code >= 400:
            raise AIUnavailable(
                f"QWEN server returned HTTP {response.status_code}: "
                f"{response.text[:200].strip()}"
            )

        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise AIUnavailable(
                f"QWEN server returned an unexpected response shape: {exc}"
            ) from exc

        return parse_verdict(content)

    def health_check(self) -> str:
        """Probe `/models` for `sentinel doctor`. Returns a status line."""
        url = f"{self.config.base_url.rstrip('/')}/models"
        headers = (
            {"Authorization": f"Bearer {self.config.api_key}"}
            if self.config.api_key
            else {}
        )
        try:
            with httpx.Client(timeout=min(self.config.timeout_seconds, 5.0)) as client:
                response = client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise AIUnavailable(f"{url} unreachable: {exc}") from exc

        if response.status_code >= 400:
            raise AIUnavailable(f"{url} returned HTTP {response.status_code}")
        return f"reachable ({self.config.model})"


def parse_verdict(content: str) -> AIVerdict:
    """Coerce a model response into an `AIVerdict`.

    Small models wrap JSON in prose or markdown fences even when told not to,
    so the object is extracted rather than assuming a clean payload.
    """
    raw = _extract_json_object(content)
    if raw is None:
        raise AIUnavailable("QWEN response contained no JSON object")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AIUnavailable(f"QWEN response was not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise AIUnavailable("QWEN response JSON was not an object")

    # Normalise before validation: the model reliably produces the right shape
    # but not always the right types.
    data["risk_level"] = Severity.parse(str(data.get("risk_level", "none")))
    data["confidence"] = _clamp_confidence(data.get("confidence"))
    for entry in data.get("packages") or []:
        if isinstance(entry, dict):
            entry["risk_level"] = Severity.parse(str(entry.get("risk_level", "none")))

    try:
        return AIVerdict.model_validate(data)
    except ValidationError as exc:
        raise AIUnavailable(f"QWEN verdict failed validation: {exc}") from exc


def _clamp_confidence(value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _extract_json_object(content: str) -> str | None:
    if not content:
        return None
    text = content.strip()

    if fenced := _FENCE_RE.search(text):
        text = fenced.group(1).strip()

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

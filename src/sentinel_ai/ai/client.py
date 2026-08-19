"""HTTP client for the on-prem model server.

Targets the OpenAI-compatible `/chat/completions` shape, which vLLM, Ollama,
llama.cpp and TGI all expose. Only `base_url` and `model` should need changing
to point at a different deployment.

The client is deliberately synchronous and single-shot: a pre-commit hook makes
one call, and async plumbing would only add startup cost to the binary.
"""

from __future__ import annotations

import json

import httpx
from pydantic import ValidationError

from ..config import AIConfig
from ..manifests import ParsedManifest
from ..models import AIVerdict, Finding, PackageChange, Severity
from .json_utils import extract_json_object, message_content, prepare_model_content
from .prompts import SYSTEM_PROMPT, build_user_prompt


class AIUnavailable(RuntimeError):
    """The server could not be reached, timed out, or returned an error.

    Whether this blocks the commit is `AIConfig.fail_open`'s decision, not
    the client's.
    """


class AIClient:
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
        if not self.config.enable_thinking:
            # Qwen3 defaults to a thinking template; reasoning text breaks JSON parsing.
            # Top level, not nested under `extra_body`: that wrapper is an OpenAI
            # *SDK* convention, and a server reading raw JSON drops the unknown key
            # and keeps on thinking.
            payload["chat_template_kwargs"] = {"enable_thinking": False}

        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        url = f"{self.config.base_url.rstrip('/')}/chat/completions"

        try:
            with httpx.Client(timeout=self.config.timeout_seconds) as client:
                response = client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise AIUnavailable(
                f"model server did not respond within "
                f"{self.config.timeout_seconds:.0f}s ({url})"
            ) from exc
        except httpx.HTTPError as exc:
            raise AIUnavailable(f"could not reach model server at {url}: {exc}") from exc

        if response.status_code >= 400:
            raise AIUnavailable(
                f"model server returned HTTP {response.status_code}: "
                f"{response.text[:200].strip()}"
            )

        try:
            body = response.json()
            choice = body["choices"][0]
            content = message_content(choice["message"])
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise AIUnavailable(
                f"model server returned an unexpected response shape: {exc}"
            ) from exc

        if not content.strip() and choice.get("finish_reason") == "length":
            raise AIUnavailable(
                "model response was truncated before producing JSON "
                f"(max_tokens={self.config.max_output_tokens}); "
                "raise ai.max_output_tokens in config"
            )

        # Truncated JSON — model output exceeded max_tokens
        if content.strip().startswith("{") and choice.get("finish_reason") == "length":
            raise AIUnavailable(
                "AI response was truncated by max_tokens limit. "
                "Either increase ai.max_output_tokens or reduce the number of packages "
                "sent to the AI model."
            )

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
    prepared = prepare_model_content(content)
    raw = extract_json_object(prepared)
    if raw is None:
        preview = prepared[:200].replace("\n", " ").strip()
        detail = f" (got: {preview!r})" if preview else ""
        raise AIUnavailable(f"model response contained no JSON object{detail}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AIUnavailable(f"model response was not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise AIUnavailable("model response JSON was not an object")

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
        raise AIUnavailable(f"model verdict failed validation: {exc}") from exc


def _clamp_confidence(value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))

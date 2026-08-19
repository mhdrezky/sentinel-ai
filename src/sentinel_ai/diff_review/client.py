"""HTTP client for the diff reviewer.

Same server and same wire quirks as `ai/client.py`, so the JSON salvage
helpers are shared. What is deliberately *not* shared is the budget: this
reviewer asks for a couple of hundred tokens on every commit, so it carries
its own `max_output_tokens` and `timeout_seconds` and only borrows the
connection details from `[ai]`.
"""

from __future__ import annotations

import json
import secrets

import httpx
from pydantic import ValidationError

from ..ai.client import AIUnavailable
from ..ai.json_utils import extract_json_object, message_content, prepare_model_content
from ..config import AIConfig, DiffReviewConfig
from ..models import Severity
from .models import DiffFinding, DiffVerdict, Verdict
from .prompts import SYSTEM_PROMPT, build_user_prompt


class DiffReviewClient:
    def __init__(self, ai: AIConfig, diff_review: DiffReviewConfig) -> None:
        self.ai = ai
        self.diff_review = diff_review

    def review(self, diff: str) -> tuple[DiffVerdict, int]:
        """Ask the model about `diff`.

        Returns the verdict and the number of entries thrown away as
        malformed. Raises `AIUnavailable` on any transport failure — the
        caller decides whether that blocks the commit.
        """
        nonce = secrets.token_hex(8)
        payload = {
            "model": self.ai.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(diff, nonce)},
            ],
            "temperature": self.ai.temperature,
            "max_tokens": self.diff_review.max_output_tokens,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        if not self.ai.enable_thinking:
            # Top level, not under `extra_body` — see `ai/client.py`. At a 256-token
            # budget a thinking model spends the whole reply reasoning and returns
            # no JSON at all.
            payload["chat_template_kwargs"] = {"enable_thinking": False}

        headers = {"Content-Type": "application/json"}
        if self.ai.api_key:
            headers["Authorization"] = f"Bearer {self.ai.api_key}"

        url = f"{self.ai.base_url.rstrip('/')}/chat/completions"
        timeout = self.diff_review.timeout_seconds

        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise AIUnavailable(
                f"model server did not respond within {timeout:.0f}s ({url})"
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

        if choice.get("finish_reason") == "length" and not _looks_complete(content):
            raise AIUnavailable(
                "model response was truncated before producing JSON "
                f"(max_tokens={self.diff_review.max_output_tokens}); "
                "raise diff_review.max_output_tokens in config"
            )

        return parse_diff_verdict(content)


def parse_diff_verdict(content: str) -> tuple[DiffVerdict, int]:
    """Coerce a model response into a `DiffVerdict`.

    Malformed findings are discarded rather than failing the whole response:
    one bad entry out of three should not cost the other two.
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

    findings: list[DiffFinding] = []
    malformed = 0
    for entry in data.get("f") or []:
        if not isinstance(entry, dict):
            malformed += 1
            continue
        # The severity ladder is ours, not the model's: anything unrecognised
        # lands on medium rather than sinking the entry.
        entry["s"] = Severity.parse(str(entry.get("s", ""))).value
        try:
            findings.append(DiffFinding.model_validate(entry))
        except ValidationError:
            malformed += 1

    return DiffVerdict(v=_verdict_of(data.get("v")), f=findings), malformed


def _verdict_of(raw: object) -> Verdict:
    try:
        return Verdict(str(raw).strip().lower())
    except ValueError:
        return Verdict.PASS


def _looks_complete(content: str) -> bool:
    """Whether a truncated-looking reply still ended up with a whole object."""
    return extract_json_object(prepare_model_content(content)) is not None

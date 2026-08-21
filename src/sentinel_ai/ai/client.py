"""Shared plumbing for the on-prem model server.

What lives here is what more than one caller needs: the failure type, and a
health probe for `sentinel-ai doctor`. The one feature that actually talks to
the model — the staged-diff review — carries its own client and its own token
and timeout budget in `diff_review/`.

Targets the OpenAI-compatible shape that vLLM, Ollama, llama.cpp and TGI all
expose. Only `base_url` and `model` should need changing to point at a
different deployment.
"""

from __future__ import annotations

import httpx

from ..config import AIConfig

# Long enough to tell "server is up" from "server is wedged", short enough that
# `doctor` stays responsive when nothing is listening.
_PROBE_TIMEOUT_SECONDS = 5.0


class AIUnavailable(RuntimeError):
    """The server could not be reached, timed out, or returned an error.

    Whether this blocks a commit is the caller's decision, not the client's.
    """


def health_check(config: AIConfig) -> str:
    """Probe `/models` for `sentinel-ai doctor`. Returns a status line."""
    url = f"{config.base_url.rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}
    try:
        with httpx.Client(timeout=_PROBE_TIMEOUT_SECONDS) as client:
            response = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise AIUnavailable(f"{url} unreachable: {exc}") from exc

    if response.status_code >= 400:
        raise AIUnavailable(f"{url} returned HTTP {response.status_code}")
    return f"reachable ({config.model})"

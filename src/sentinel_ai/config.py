"""Configuration, resolved from (in ascending precedence):

1. built-in defaults
2. `.sentinel.toml` in the scanned repository root
3. `SENTINEL_*` environment variables

Every knob has a working default so a fresh repo needs no config file at all.
"""

from __future__ import annotations

import os
import shutil
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field

from .models import Severity

CONFIG_FILENAME = ".sentinel.toml"


class AIConfig(BaseModel):
    """On-prem model server.

    Assumes an OpenAI-compatible `/chat/completions` endpoint, which is what
    vLLM, Ollama, llama.cpp, and TGI all expose. Point `base_url` and `model`
    at your deployment.
    """

    enabled: bool = True
    base_url: str = "http://localhost:8000/v1"
    model: str = "local-model"
    api_key: str | None = None
    timeout_seconds: float = 20.0
    max_output_tokens: int = 768
    temperature: float = 0.0
    fail_open: bool = True
    """If the server is unreachable, warn and continue rather than block.

    Left on by default so an AI outage cannot freeze every developer's commits.
    Turn off for hardened environments where an unverifiable package must fail.
    """


class TrivyConfig(BaseModel):
    enabled: bool = True
    binary_path: str = "trivy"
    timeout_seconds: float = 60.0
    offline: bool = False
    """Pass `--offline-scan` and skip DB refresh; faster but may miss new CVEs."""
    skip_db_update: bool = False

    def resolve_binary(self) -> str | None:
        """Absolute path to the Trivy binary, or None when it is not installed."""
        return shutil.which(self.binary_path)


class PolicyConfig(BaseModel):
    """What actually fails a commit."""

    block_at_or_above: Severity = Severity.HIGH
    block_on_install_scripts: bool = True
    """npm pre/post-install hooks on a *newly added* package are high risk."""
    block_on_nonregistry_source: bool = True
    """git://, http(s):// tarballs, and local file: specs bypass registry vetting."""
    allowlist: list[str] = Field(default_factory=list)
    """Package names or `ecosystem:name` coordinates that are always permitted."""
    denylist: list[str] = Field(default_factory=list)
    max_packages_before_ai_batching: int = 25

    def is_allowlisted(self, name: str, coordinate: str) -> bool:
        bare = coordinate.split("@")[0] if ":" in coordinate else name
        return any(entry in (name, bare, coordinate) for entry in self.allowlist)


class Settings(BaseModel):
    repo_root: Path = Field(default_factory=Path.cwd)
    ai: AIConfig = Field(default_factory=AIConfig)
    trivy: TrivyConfig = Field(default_factory=TrivyConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    verbose: bool = False

    @classmethod
    def load(cls, repo_root: Path | None = None) -> Settings:
        root = (repo_root or Path.cwd()).resolve()
        data: dict = {"repo_root": root}

        config_path = root / CONFIG_FILENAME
        if config_path.is_file():
            try:
                data.update(tomllib.loads(config_path.read_text(encoding="utf-8")))
            except (tomllib.TOMLDecodeError, OSError) as exc:
                raise ConfigError(f"Could not read {config_path}: {exc}") from exc
        data["repo_root"] = root

        settings = cls.model_validate(data)
        settings._apply_env_overrides()
        return settings

    def _apply_env_overrides(self) -> None:
        """`SENTINEL_AI_BASE_URL` beats the file, so CI/agents can redirect it."""
        env = os.environ

        if (base_url := env.get("SENTINEL_AI_BASE_URL")) is not None:
            self.ai.base_url = base_url
        if (model := env.get("SENTINEL_AI_MODEL")) is not None:
            self.ai.model = model
        if (api_key := env.get("SENTINEL_AI_API_KEY")) is not None:
            self.ai.api_key = api_key
        if (timeout := _env_float(env, "SENTINEL_AI_TIMEOUT")) is not None:
            self.ai.timeout_seconds = timeout
        if (enabled := _env_bool(env, "SENTINEL_AI_ENABLED")) is not None:
            self.ai.enabled = enabled

        if (trivy_bin := env.get("SENTINEL_TRIVY_PATH")) is not None:
            self.trivy.binary_path = trivy_bin
        if (trivy_on := _env_bool(env, "SENTINEL_TRIVY_ENABLED")) is not None:
            self.trivy.enabled = trivy_on

        if (threshold := env.get("SENTINEL_BLOCK_AT")) is not None:
            self.policy.block_at_or_above = Severity.parse(threshold)
        if (verbose := _env_bool(env, "SENTINEL_VERBOSE")) is not None:
            self.verbose = verbose


class ConfigError(RuntimeError):
    """Raised when a present-but-broken config file is found."""


_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def _env_bool(env: dict[str, str] | os._Environ, key: str) -> bool | None:
    raw = env.get(key)
    if raw is None:
        return None
    normalised = raw.strip().lower()
    if normalised in _TRUTHY:
        return True
    if normalised in _FALSY:
        return False
    return None


def _env_float(env: dict[str, str] | os._Environ, key: str) -> float | None:
    raw = env.get(key)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None

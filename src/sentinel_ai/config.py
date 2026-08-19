"""Configuration, resolved from (in ascending precedence):

1. built-in defaults (pydantic Field defaults)
2. bundled defaults shipped with the package (`src/sentinel_ai/sentinel.toml`)
3. host override at `~/.sentinel-ai/config.toml` (created by install.ps1 / install.sh on first run)
4. `SENTINEL_CONFIG` environment variable (optional explicit path)
5. `SENTINEL_*` environment variables

Edit the host file after install: `sentinel-ai config edit`.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field

from .models import Severity

BUNDLED_CONFIG_NAME = "sentinel.toml"
HOST_CONFIG_DIRNAME = ".sentinel-ai"
HOST_CONFIG_FILENAME = "config.toml"


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
    max_output_tokens: int = 2048
    temperature: float = 0.0
    fail_open: bool = True
    """If the server is unreachable, warn and continue rather than block.

    Left on by default so an AI outage cannot freeze every developer's commits.
    Turn off for hardened environments where an unverifiable package must fail.
    """
    enable_thinking: bool = False
    """Qwen3/vLLM thinking mode. Off by default — thinking output breaks JSON parsing."""


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


class DiffReviewConfig(BaseModel):
    """Staged-diff review — a separate layer from the dependency check.

    Token and timeout budgets are deliberately *not* inherited from `AIConfig`:
    this reviewer asks for a tiny JSON verdict and runs on every commit, so
    tuning the dependency reviewer must not silently move its limits.
    """

    enabled: bool = True
    fail_open: bool = True
    """An unreachable model server warns and lets the commit through."""
    max_output_tokens: int = 256
    timeout_seconds: float = 12.0
    max_diff_bytes: int = 40_000
    """Diffs above this skip the model entirely — truncating would hide findings."""
    log_file: str = "ai-review.jsonl"
    """Resolved against the repository's git directory, not `repo_root / .git`."""
    log_findings: bool = False


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
    diff_review: DiffReviewConfig = Field(default_factory=DiffReviewConfig)
    verbose: bool = False

    @classmethod
    def load(cls) -> Settings:
        data: dict = {}
        for config_path in _global_config_paths():
            data = _deep_merge(data, _read_toml(config_path))

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

        if (dr_on := _env_bool(env, "SENTINEL_DIFF_REVIEW_ENABLED")) is not None:
            self.diff_review.enabled = dr_on
        if (dr_tokens := _env_int(env, "SENTINEL_DIFF_REVIEW_MAX_TOKENS")) is not None:
            self.diff_review.max_output_tokens = dr_tokens
        if (dr_timeout := _env_float(env, "SENTINEL_DIFF_REVIEW_TIMEOUT")) is not None:
            self.diff_review.timeout_seconds = dr_timeout

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


def host_config_path() -> Path:
    """Per-machine override written by scripts/install.ps1 or scripts/install.sh."""
    return host_data_dir() / HOST_CONFIG_FILENAME


def host_data_dir() -> Path:
    """Sentinel-AI state on this machine (config, bundled Trivy binary, etc.)."""
    return Path.home() / HOST_CONFIG_DIRNAME


def ensure_host_config() -> Path:
    """Create host config from bundled defaults when it does not exist yet."""
    path = host_config_path()
    if path.is_file():
        return path

    bundled = _bundled_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if bundled.is_file():
        path.write_text(bundled.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    else:
        path.write_text("", encoding="utf-8")
    return path


def open_host_config_in_editor() -> Path:
    """Open the host config in the platform default editor."""
    path = ensure_host_config()
    try:
        if sys.platform == "win32":
            subprocess.run(["notepad.exe", str(path)], check=False)
        elif sys.platform == "darwin":
            subprocess.run(["open", "-t", str(path)], check=True)
        else:
            editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
            if editor:
                subprocess.run([*shlex.split(editor), str(path)], check=False)
            elif shutil.which("xdg-open"):
                subprocess.run(["xdg-open", str(path)], check=False)
            elif shutil.which("nano"):
                subprocess.run(["nano", str(path)], check=False)
            else:
                raise ConfigError(
                    "No editor found. Set $EDITOR or install xdg-open/nano."
                )
    except OSError as exc:
        raise ConfigError(f"Could not open {path}: {exc}") from exc
    return path


def _bundled_config_path() -> Path:
    """Default config shipped inside the installed package."""
    return Path(__file__).resolve().parent / BUNDLED_CONFIG_NAME


def resolved_config_paths() -> list[Path]:
    """Config files on disk that contribute to Settings.load(), in merge order."""
    return [path for path in _global_config_paths() if path.is_file()]


def _global_config_paths() -> list[Path]:
    """Global config locations, checked in order."""
    paths: list[Path] = [_bundled_config_path(), host_config_path()]
    if env_path := os.environ.get("SENTINEL_CONFIG"):
        paths.append(Path(env_path).expanduser())
    return paths


def _read_toml(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        # utf-8-sig, not utf-8: Windows PowerShell 5.1 `Set-Content -Encoding utf8`
        # writes a BOM, and a stray ﻿ makes tomllib fail at line 1, column 1.
        return tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Merge nested dicts so `[ai]` in one file can override just `base_url`."""
    result = dict(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


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


def _env_int(env: dict[str, str] | os._Environ, key: str) -> int | None:
    raw = env.get(key)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None

"""Self-update via `uv tool install` from the latest GitHub release."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

GITHUB_REPO = "mhdrezky/sentinel-ai"
RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
INSTALL_TIMEOUT = 300


class UpdateError(Exception):
    """Update could not complete."""


def fetch_latest_release_tag() -> str:
    request = Request(RELEASES_API, headers={"User-Agent": "sentinel-ai-update"})
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except URLError as exc:
        raise UpdateError(f"could not reach GitHub releases API: {exc}") from exc
    tag = payload.get("tag_name")
    if not tag:
        raise UpdateError("GitHub releases/latest returned no tag_name")
    return str(tag)


def run_update(*, source: Path | None = None) -> str:
    """Install the latest release or a local checkout. Returns the installed ref."""
    if source is not None:
        resolved = source.resolve()
        if not resolved.is_dir():
            raise UpdateError(f"source path not found: {source}")
        _uv_tool_install(["--from", str(resolved), "sentinel-ai"])
        return str(resolved)

    tag = fetch_latest_release_tag()
    _uv_tool_install([f"git+https://github.com/{GITHUB_REPO}.git@{tag}"])
    return tag


def _uv_tool_install(install_args: list[str]) -> None:
    uv = shutil.which("uv")
    if not uv:
        raise UpdateError(
            "uv is not on PATH. Re-run the Sentinel-AI installer or install uv "
            "from https://docs.astral.sh/uv/"
        )

    command = [uv, "tool", "install", "--force", *install_args]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=INSTALL_TIMEOUT,
            check=False,
        )
    except OSError as exc:
        raise UpdateError(f"failed to run uv: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise UpdateError("uv tool install timed out") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise UpdateError(f"uv tool install failed{suffix}")

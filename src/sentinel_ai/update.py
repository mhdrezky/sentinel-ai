"""Self-update via `uv tool install` from the latest GitHub release."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

GITHUB_REPO = "mhdrezky/sentinel-ai"
RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
INSTALL_TIMEOUT = 300


class UpdateError(Exception):
    """Update could not complete."""


def running_from_uv_tool_env() -> Path | None:
    """The uv tool environment this process runs out of, if it is one.

    `uv-receipt.toml` is written by uv into the environment root, so its
    presence beside `sys.prefix` is the reliable signal.
    """
    prefix = Path(sys.prefix)
    return prefix if (prefix / "uv-receipt.toml").is_file() else None


def _refuse_self_replacement(install_args: list[str] | None) -> None:
    """Windows cannot replace the environment it is executing from.

    The running `python.exe` lives in the tool environment's `Scripts`
    directory and Windows locks it, but uv deletes `Lib/site-packages` before
    it reaches that lock — so a failed self-update does not leave the old
    version in place, it leaves no version at all. Refusing costs the user one
    copied command; not refusing costs them a working CLI.

    Called before the release lookup, so the refusal does not depend on GitHub
    being reachable — an unauthenticated API call is rate-limited often enough
    that the answer would otherwise be "could not reach GitHub" on a machine
    that was never going to be able to update anyway.
    """
    if sys.platform != "win32" or running_from_uv_tool_env() is None:
        return

    installer = (
        "https://github.com/mhdrezky/sentinel-ai/releases/latest/download/install.ps1"
    )
    manual = (
        f"uv tool install --force {' '.join(install_args)}"
        if install_args
        else f"uv tool install --force git+https://github.com/{GITHUB_REPO}.git@TAG"
    )
    raise UpdateError(
        "Sentinel-AI cannot update itself on Windows: this command runs from "
        "inside the environment uv would replace, and a partial replacement "
        "leaves the CLI unusable. Run this from a normal terminal instead — "
        f"irm {installer} | iex — or {manual}"
    )


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
        args = ["--from", str(resolved), "sentinel-ai"]
        _refuse_self_replacement(args)
        _uv_tool_install(args)
        return str(resolved)

    _refuse_self_replacement(None)
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

"""Remove Sentinel-AI from the host."""

from __future__ import annotations

import shutil
import subprocess

from .config import host_data_dir

TOOL_NAME = "sentinel-ai"
UNINSTALL_TIMEOUT = 120


class UninstallError(Exception):
    """Uninstall could not complete."""


def uninstall_targets() -> list[str]:
    """Human-readable paths that `run_uninstall` removes."""
    targets = [str(host_data_dir())]
    if _uv_binary():
        targets.append(f"uv tool: {TOOL_NAME}")
    else:
        targets.append(f"uv tool: {TOOL_NAME} (uv not on PATH — remove manually if present)")
    return targets


def run_uninstall(*, yes: bool) -> list[str]:
    """Remove host data and uninstall the uv tool. Returns removed items."""
    if not yes:
        preview = ", ".join(uninstall_targets())
        raise UninstallError(f"pass --yes to confirm removal of {preview}")

    removed: list[str] = []
    host_dir = host_data_dir()
    if host_dir.exists():
        shutil.rmtree(host_dir)
        removed.append(str(host_dir))

    uv = _uv_binary()
    if uv is None:
        return removed

    try:
        completed = subprocess.run(
            [uv, "tool", "uninstall", TOOL_NAME],
            capture_output=True,
            text=True,
            timeout=UNINSTALL_TIMEOUT,
            check=False,
        )
    except OSError as exc:
        raise UninstallError(f"failed to run uv: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise UninstallError("uv tool uninstall timed out") from exc

    detail = (completed.stderr or completed.stdout or "").strip()
    if completed.returncode == 0:
        removed.append(f"uv tool: {TOOL_NAME}")
        return removed
    if completed.returncode == 2 and "is not installed" in detail.lower():
        return removed

    suffix = f": {detail}" if detail else ""
    raise UninstallError(f"uv tool uninstall failed{suffix}")


def _uv_binary() -> str | None:
    return shutil.which("uv")

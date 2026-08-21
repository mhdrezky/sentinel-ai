"""Remove Sentinel-AI from the host."""

from __future__ import annotations

import shutil
import subprocess
import sys

from .config import host_data_dir
from .update import running_from_uv_tool_env

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
        targets.append(
            f"uv tool: {TOOL_NAME} (uv not on PATH — remove manually if present)"
        )
    return targets


def run_uninstall(*, yes: bool) -> list[str]:
    """Remove host data and uninstall the uv tool. Returns removed items."""
    if not yes:
        preview = ", ".join(uninstall_targets())
        raise UninstallError(f"pass --yes to confirm removal of {preview}")

    removed: list[str] = []

    # Before the directory goes: the global hook lives inside it, and git would
    # otherwise be left pointing core.hooksPath at a path that no longer exists.
    removed.extend(_remove_global_hook())

    host_dir = host_data_dir()
    if host_dir.exists():
        shutil.rmtree(host_dir)
        removed.append(str(host_dir))

    uv = _uv_binary()
    if uv is None:
        return removed

    if sys.platform == "win32" and running_from_uv_tool_env() is not None:
        # Same lock as the self-update path: Windows will not let uv delete the
        # `Scripts` directory this interpreter is running from. Everything else
        # is already gone, so say what is left rather than failing over it.
        raise UninstallError(
            "Removed host data, but the CLI itself cannot uninstall itself on "
            "Windows. Finish with: uv tool uninstall sentinel-ai"
        )

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


def _remove_global_hook() -> list[str]:
    """Undo `install-global-hook`, if this machine has it."""
    from .globalhook import uninstall as uninstall_global_hook

    try:
        return uninstall_global_hook()
    except Exception:
        # Uninstalling must not fail over a hook that was never installed or a
        # git that is no longer on PATH.
        return []

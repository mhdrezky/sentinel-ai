"""The machine-wide pre-commit hook.

Git never installs hooks on clone — cloning a repository must not be able to
run its code — so a hook has to be set up once per working copy. With a dozen
repositories across ten machines that is a hundred acts of remembering, and it
does not happen: the CLI ends up installed everywhere while the gate runs
almost nowhere.

Setting `core.hooksPath` globally moves that from once-per-repository to
once-per-machine, which is the same unit the CLI itself is installed at.

The organisation guard is written *into* the generated script rather than read
from config at commit time, and that is deliberate: starting the CLI costs
roughly 600ms, while the shell test costs about 135ms. Every commit in every
personal repository on the machine pays whichever one it is, and 600ms of tax
on unrelated work is how a hook earns itself a `--no-verify` habit.
"""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path

from . import gitdiff
from .config import host_data_dir

HOOKS_PATH_KEY = "core.hooksPath"
_MARKER = "# sentinel-ai:orgs="
_SOURCE_MARKER = "# sentinel-ai:source="
_ALL = "*"


class GlobalHookError(RuntimeError):
    """The hook could not be installed or removed."""


def hooks_dir() -> Path:
    return host_data_dir() / "hooks"


def hook_path() -> Path:
    return hooks_dir() / "pre-commit"


@dataclass
class GlobalHookStatus:
    """What is actually on the machine, for `doctor` to report."""

    installed: bool
    hooks_path: str | None
    """Whatever `core.hooksPath` currently points at, ours or not."""
    installed_organizations: list[str] | None
    """Baked into the script. None means the script covers every repository."""
    installed_from_config: bool
    """The list came from `hook.organizations` rather than from `--org`/`--all`."""
    points_elsewhere: bool
    """`core.hooksPath` is set, but not to our directory."""

    def drifted_from(self, configured: list[str]) -> bool:
        """Config has been edited since a config-driven install.

        Worth surfacing: editing `organizations` looks like it should take
        effect immediately, and nothing about the commit output would reveal
        that the script still carries the old list.

        Only meaningful when config was the source. An install that named its
        organisations with `--org` never consulted config, so a config that
        says something else is not drift — and telling that user to re-run
        without the flag would send them into "no organisations configured".
        Scripts written before this was recorded report no drift at all, which
        is the safe answer when we cannot tell.
        """
        if not self.installed or not self.installed_from_config:
            return False
        if self.installed_organizations is None:
            return False
        return sorted(self.installed_organizations) != sorted(configured)


def render_hook(organizations: list[str], *, from_config: bool = False) -> str:
    """The script git will run. An empty list means every repository."""
    marker = ",".join(organizations) if organizations else _ALL
    lines = [
        "#!/bin/sh",
        "# Managed by Sentinel-AI. Regenerate with `sentinel-ai install-global-hook`.",
        f"{_MARKER}{marker}",
        f"{_SOURCE_MARKER}{'config' if from_config else 'flags'}",
        "",
    ]

    if organizations:
        patterns = "|".join(f"*{org}*" for org in organizations)
        lines += [
            "# Stay out of repositories that are not ours. This runs before the CLI",
            "# starts, so an unrelated commit costs a few milliseconds, not a second.",
            'case "$(git config --get remote.origin.url)" in',
            f"  {patterns}) ;;",
            "  *) exit 0 ;;",
            "esac",
            "",
        ]

    lines += ["exec sentinel-ai check", ""]
    return "\n".join(lines)


def parse_organizations(script: str) -> list[str] | None:
    """Read the org list back out of an installed script."""
    for line in script.splitlines():
        if line.startswith(_MARKER):
            value = line[len(_MARKER) :].strip()
            if value == _ALL:
                return []
            return [part for part in value.split(",") if part]
    return None


def parse_source(script: str) -> str | None:
    """Whether the installed list came from config or from flags."""
    for line in script.splitlines():
        if line.startswith(_SOURCE_MARKER):
            return line[len(_SOURCE_MARKER) :].strip()
    return None


def status() -> GlobalHookStatus:
    configured_path = gitdiff.global_config(HOOKS_PATH_KEY)
    ours = _same_path(configured_path, hooks_dir())
    script = hook_path().read_text(encoding="utf-8") if hook_path().is_file() else None

    return GlobalHookStatus(
        installed=bool(ours and script is not None),
        hooks_path=configured_path,
        installed_organizations=parse_organizations(script) if script else None,
        installed_from_config=bool(script and parse_source(script) == "config"),
        points_elsewhere=bool(configured_path and not ours),
    )


def install(
    organizations: list[str], *, force: bool = False, from_config: bool = False
) -> Path:
    """Write the hook and point git's global `core.hooksPath` at it."""
    current = gitdiff.global_config(HOOKS_PATH_KEY)
    if current and not _same_path(current, hooks_dir()) and not force:
        raise GlobalHookError(
            f"{HOOKS_PATH_KEY} is already set to {current}. "
            f"Re-run with --force to replace it."
        )

    target = hook_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        render_hook(organizations, from_config=from_config),
        encoding="utf-8",
        newline="\n",
    )
    # Git checks the executable bit before running a hook.
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # Forward slashes: git stores the value verbatim and a Windows path with
    # backslashes comes back out escaped.
    gitdiff.set_global_config(HOOKS_PATH_KEY, hooks_dir().as_posix())
    return target


def uninstall() -> list[str]:
    """Undo `install`. Leaves a `core.hooksPath` that points somewhere else."""
    removed: list[str] = []

    if _same_path(gitdiff.global_config(HOOKS_PATH_KEY), hooks_dir()):
        gitdiff.unset_global_config(HOOKS_PATH_KEY)
        removed.append(f"git config --global {HOOKS_PATH_KEY}")

    target = hook_path()
    if target.is_file():
        target.unlink()
        removed.append(str(target))

    return removed


def _same_path(value: str | None, path: Path) -> bool:
    if not value:
        return False
    try:
        return Path(value).expanduser().resolve() == path.resolve()
    except OSError:
        return False

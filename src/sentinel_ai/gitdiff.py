"""Thin wrapper over the git plumbing Sentinel-AI needs.

The pre-commit hook runs against the *index*, not the working tree, so we read
staged blobs (`git show :path`) and compare them to HEAD (`git show HEAD:path`).
That way a developer who edits a file after `git add` cannot smuggle a package
past the check.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

GIT_TIMEOUT_SECONDS = 15.0


class GitError(RuntimeError):
    """git was missing, timed out, or returned an unexpected failure."""


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitError("git executable not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args)} timed out") from exc


def repo_root(start: Path | None = None) -> Path:
    """Top level of the working tree containing `start`."""
    cwd = (start or Path.cwd()).resolve()
    result = _run_git(["rev-parse", "--show-toplevel"], cwd)
    if result.returncode != 0:
        raise GitError(f"not inside a git repository: {cwd}")
    return Path(result.stdout.strip()).resolve()


def has_head(root: Path) -> bool:
    """False on a repo whose first commit has not landed yet."""
    return _run_git(["rev-parse", "--verify", "HEAD"], root).returncode == 0


def staged_files(root: Path) -> list[str]:
    """Repo-relative paths of files added/copied/modified/renamed in the index.

    Deletions are excluded — removing a dependency is never a supply-chain risk.
    """
    args = ["diff", "--cached", "--name-only", "--diff-filter=ACMR"]
    if not has_head(root):
        # No HEAD yet: diff against the empty tree so the initial commit is scanned.
        args = ["diff", "--cached", "--name-only", "--diff-filter=ACMR", _EMPTY_TREE]
    result = _run_git(args, root)
    if result.returncode != 0:
        raise GitError(f"could not list staged files: {result.stderr.strip()}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def changed_files_between(root: Path, base: str, head: str = "HEAD") -> list[str]:
    """Paths changed between two refs — for `--range` runs outside the hook."""
    result = _run_git(
        ["diff", "--name-only", "--diff-filter=ACMR", f"{base}...{head}"], root
    )
    if result.returncode != 0:
        raise GitError(f"could not diff {base}...{head}: {result.stderr.strip()}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def read_staged(root: Path, path: str) -> str | None:
    """Staged content of `path`, or None when it is not in the index."""
    return _read_blob(root, f":{path}")


def read_committed(root: Path, path: str, ref: str = "HEAD") -> str | None:
    """Content of `path` at `ref`, or None when it did not exist there.

    A None here means the manifest itself is new, so every dependency in the
    staged version counts as freshly added.
    """
    if ref == "HEAD" and not has_head(root):
        return None
    return _read_blob(root, f"{ref}:{path}")


def read_worktree(root: Path, path: str) -> str | None:
    """On-disk content — used by `--all`, which ignores the index entirely."""
    target = root / path
    try:
        return target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _read_blob(root: Path, spec: str) -> str | None:
    result = _run_git(["show", spec], root)
    if result.returncode != 0:
        return None
    return result.stdout


# git's canonical empty tree object; diffing against it yields "everything added".
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

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


def global_config(key: str) -> str | None:
    """Read a value from the user's global git config, or None when unset."""
    result = _run_git(["config", "--global", "--get", key], Path.home())
    value = result.stdout.strip()
    return value or None


def set_global_config(key: str, value: str) -> None:
    result = _run_git(["config", "--global", key, value], Path.home())
    if result.returncode != 0:
        raise GitError(f"could not set git config {key}: {result.stderr.strip()}")


def unset_global_config(key: str) -> None:
    """Remove a key. A key that was already absent is not an error."""
    result = _run_git(["config", "--global", "--unset", key], Path.home())
    # 5 is git's "key not found"; anything else is a real failure.
    if result.returncode not in (0, 5):
        raise GitError(f"could not unset git config {key}: {result.stderr.strip()}")


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


def git_dir(root: Path) -> Path:
    """The repository's git directory.

    Not `root / ".git"`: in a linked worktree or a submodule that path is a
    *file* pointing elsewhere, so anything written there would land in the
    wrong place — or nowhere.
    """
    result = _run_git(["rev-parse", "--git-dir"], root)
    if result.returncode != 0:
        raise GitError(f"could not resolve the git directory for {root}")
    raw = Path(result.stdout.strip())
    return raw.resolve() if raw.is_absolute() else (root / raw).resolve()


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


def staged_unified_diff(root: Path) -> str:
    """Unified diff of the index — the text the diff reviewer reads.

    `--no-ext-diff` because a `diff.external` driver in the developer's
    gitconfig would emit something that is not a unified diff at all, and the
    grounding pass would reject every finding. `--diff-filter=ACMR` matches
    `staged_files`: removed lines from a deleted file are noise here.
    """
    args = [
        "diff",
        "--cached",
        "--no-color",
        "--no-ext-diff",
        "--diff-filter=ACMR",
    ]
    if not has_head(root):
        # No HEAD yet: diff against the empty tree so the initial commit is reviewed.
        args.append(_EMPTY_TREE)
    result = _run_git(args, root)
    if result.returncode != 0:
        raise GitError(f"could not read the staged diff: {result.stderr.strip()}")
    return result.stdout


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

"""Tests for `sentinel-ai install-hook`."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sentinel_ai.decision_engine import EXIT_ERROR, EXIT_PASS
from sentinel_ai.main import _append_hook_line, main

_HOOK_LINE = "sentinel-ai check || exit 1"


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "app"
    root.mkdir()
    git(root, "init")
    git(root, "config", "user.email", "test@example.local")
    git(root, "config", "user.name", "Test")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "commit", "--allow-empty", "-m", "init")
    return root


def write_pre_commit(repo: Path, content: str) -> Path:
    husky = repo / ".husky"
    husky.mkdir()
    hook = husky / "pre-commit"
    hook.write_text(content, encoding="utf-8", newline="\n")
    return hook


class TestAppendHookLine:
    def test_appends_after_existing_content(self):
        result = _append_hook_line("#!/usr/bin/env sh\nnpm test\n")
        assert result == f"#!/usr/bin/env sh\nnpm test\n{_HOOK_LINE}\n"

    def test_empty_file_gets_hook_only(self):
        assert _append_hook_line("") == f"{_HOOK_LINE}\n"


class TestInstallHook:
    def test_requires_husky_directory(self, repo: Path):
        assert main(["install-hook", "--repo", str(repo)]) == EXIT_ERROR

    def test_appends_to_existing_pre_commit(self, repo: Path):
        hook = write_pre_commit(repo, "#!/usr/bin/env sh\nnpm test\n")
        assert main(["install-hook", "--repo", str(repo)]) == EXIT_PASS
        assert (
            hook.read_text(encoding="utf-8")
            == f"#!/usr/bin/env sh\nnpm test\n{_HOOK_LINE}\n"
        )

    def test_reports_when_already_present(self, repo: Path):
        hook = write_pre_commit(repo, f"#!/usr/bin/env sh\n{_HOOK_LINE}\n")
        before = hook.read_text(encoding="utf-8")
        assert main(["install-hook", "--repo", str(repo)]) == EXIT_PASS
        assert hook.read_text(encoding="utf-8") == before

    def test_creates_pre_commit_when_missing(self, repo: Path):
        husky = repo / ".husky"
        husky.mkdir()
        hook = husky / "pre-commit"
        assert main(["install-hook", "--repo", str(repo)]) == EXIT_PASS
        assert hook.read_text(encoding="utf-8") == f"{_HOOK_LINE}\n"

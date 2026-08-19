"""Wire types for the diff reviewer, plus the JSONL trial log.

The model is asked for the smallest useful object — a verdict and a list of
findings, nothing else — because every field it generates costs latency on a
hook that runs on each commit. Those short wire names (`v`, `f`, `c`, `s`,
`snip`) are the contract with the model; the Python side reads them through
aliases so the rest of the package can use words.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..models import Severity


class DiffCategory(StrEnum):
    """What a finding is about. Phase 1 adds four more values here."""

    NETWORK = "network"
    WATERMARK = "watermark"


class Verdict(StrEnum):
    PASS = "pass"
    NOTICE = "notice"
    BLOCK = "block"


class SkipReason(StrEnum):
    """Why a run never reached the model. `null` in the log when it did."""

    NO_AI = "no_ai"
    DIFF_TOO_LARGE = "diff_too_large"
    MODEL_UNAVAILABLE = "model_unavailable"
    NO_REVIEWABLE_DIFF = "no_reviewable_diff"
    DISABLED = "disabled"


class DiffFinding(BaseModel):
    """One reported problem, still unverified until grounding accepts it."""

    model_config = ConfigDict(populate_by_name=True)

    category: DiffCategory = Field(alias="c")
    severity: Severity = Field(alias="s")
    file: str
    line: int
    snippet: str = Field(alias="snip")

    def as_wire(self) -> dict:
        return self.model_dump(by_alias=True, mode="json")


class DiffVerdict(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    verdict: Verdict = Field(default=Verdict.PASS, alias="v")
    findings: list[DiffFinding] = Field(default_factory=list, alias="f")


def append_log(path: Path, record: dict) -> None:
    """Append one JSONL line, or give up quietly.

    A trial log that breaks a developer's commit would be worse than a trial
    log with a hole in it, so every filesystem failure here is swallowed.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError):
        return


def log_record(
    *,
    verdict: Verdict,
    grounded: int,
    dropped: int,
    elapsed_ms: int,
    diff_bytes: int,
    skipped: SkipReason | None,
    findings: list[DiffFinding] | None = None,
) -> dict:
    """The one line a run contributes to the trial log."""
    record: dict = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "v": verdict.value,
        "n": grounded,
        "drop": dropped,
        "ms": elapsed_ms,
        "bytes": diff_bytes,
        "ai_skipped": skipped.value if skipped else None,
    }
    if findings is not None:
        record["f"] = [finding.as_wire() for finding in findings]
    return record

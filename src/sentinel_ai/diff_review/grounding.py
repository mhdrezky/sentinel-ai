"""Verifying that a finding points at text the commit actually adds.

A small model will occasionally describe a problem that is not in the diff at
all, so nothing reaches the developer until its snippet is found among the
added lines. The match is whitespace-normalised rather than literal: models
reflow and re-indent what they quote, and demanding byte equality throws away
real findings — which is the failure this whole layer is being trialled for.
Case is preserved, because hostnames and URLs differ by it.
"""

from __future__ import annotations

import re

from .diff import _split_by_file
from .models import DiffFinding

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_WHITESPACE_RE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Collapse whitespace runs so quoting differences stop mattering."""
    return _WHITESPACE_RE.sub(" ", text).strip()


def added_lines(diff: str) -> dict[str, list[tuple[int, str]]]:
    """Map each file to its added lines as `(new-file line number, text)`."""
    result: dict[str, list[tuple[int, str]]] = {}
    for path, section in _split_by_file(diff):
        result.setdefault(path, []).extend(_added_in_section(section))
    return result


def _added_in_section(section: str) -> list[tuple[int, str]]:
    added: list[tuple[int, str]] = []
    line_number = 0
    in_hunk = False

    for line in section.splitlines():
        if hunk := _HUNK_RE.match(line):
            line_number = int(hunk.group(1))
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added.append((line_number, line[1:]))
            line_number += 1
        elif line.startswith("-") or line.startswith("\\"):
            # Removed lines and "\ No newline at end of file" do not advance
            # the new-file counter.
            continue
        else:
            line_number += 1

    return added


def ground(findings: list[DiffFinding], diff: str) -> tuple[list[DiffFinding], int]:
    """Keep findings whose snippet appears in an added line; count the rest.

    A finding whose snippet matches but whose line number does not is kept
    with the line corrected: the evidence is real and the number is the part
    models get wrong most often.
    """
    index = added_lines(diff)
    kept: list[DiffFinding] = []
    dropped = 0

    for finding in findings:
        matched = _match(finding, index.get(finding.file, []))
        if matched is None:
            dropped += 1
            continue
        kept.append(matched)

    return kept, dropped


def _match(finding: DiffFinding, candidates: list[tuple[int, str]]) -> DiffFinding | None:
    needle = normalise(finding.snippet)
    if not needle or not candidates:
        return None

    for number, text in candidates:
        if number == finding.line and needle in normalise(text):
            return finding

    for number, text in candidates:
        if needle in normalise(text):
            return finding.model_copy(update={"line": number})

    return None

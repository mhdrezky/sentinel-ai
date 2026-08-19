"""Collecting the staged diff and deciding whether it is worth reviewing.

Dependency manifests and lockfiles are stripped out: `sentinel-ai check`
already reviews those, and feeding a regenerated lockfile to this reviewer
would spend the whole token budget on noise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .. import manifests
from ..gitdiff import staged_unified_diff

_FILE_HEADER = "diff --git "


@dataclass
class StagedDiff:
    """The reviewable part of the index."""

    text: str = ""
    files: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)

    @property
    def size_bytes(self) -> int:
        return len(self.text.encode("utf-8"))

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


def collect(root: Path) -> StagedDiff:
    """Staged diff with manifests and lockfiles removed."""
    raw = staged_unified_diff(root)
    if not raw.strip():
        return StagedDiff()

    kept: list[str] = []
    files: list[str] = []
    excluded: list[str] = []
    for path, section in _split_by_file(raw):
        if manifests.identify(path) is not None:
            excluded.append(path)
            continue
        kept.append(section)
        files.append(path)

    return StagedDiff(text="".join(kept), files=files, excluded=excluded)


def _split_by_file(diff: str) -> list[tuple[str, str]]:
    """Break a unified diff into `(new path, section text)` pairs."""
    sections: list[tuple[str, str]] = []
    current: list[str] = []
    path = ""

    for line in diff.splitlines(keepends=True):
        if line.startswith(_FILE_HEADER):
            if current:
                sections.append((path, "".join(current)))
            current = [line]
            path = _path_from_header(line)
            continue
        if current:
            current.append(line)

    if current:
        sections.append((path, "".join(current)))
    return sections


def _path_from_header(line: str) -> str:
    """`diff --git a/src/app.py b/src/app.py` -> `src/app.py`.

    The b-side is the one that matters: a rename reports its new name, and a
    finding is always about the file as it will exist after the commit.
    """
    remainder = line[len(_FILE_HEADER) :].strip()
    marker = " b/"
    index = remainder.rfind(marker)
    if index == -1:
        return remainder
    return remainder[index + len(marker) :].strip().strip('"')

"""Collects raw evidence about the dependencies a commit introduces.

The scanner answers "what changed and what is known about it", and nothing
more — it never decides whether to block. That belongs to `decision_engine`.

Pipeline:
    staged files -> manifests -> package changes -> heuristics + Trivy
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import gitdiff, heuristics, manifests
from .config import Settings
from .manifests import ParsedManifest
from .models import Finding, FindingSource, PackageChange, ScanResult, Severity


@dataclass
class SourceRevision:
    """Which pair of revisions to compare.

    `staged` is the pre-commit default. `worktree` ignores the index (for
    `--all` runs), and `range` compares two refs.
    """

    mode: str = "staged"
    base_ref: str = "HEAD"
    head_ref: str = "HEAD"


class Scanner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = settings.repo_root
        self.parsed_manifests: dict[str, ParsedManifest] = {}
        """Staged manifests from the last `scan()`.

        Kept here rather than on ScanResult because the AI layer needs the raw
        parse (lifecycle script bodies) while ScanResult stays serialisable.
        """

    def scan(self, revision: SourceRevision | None = None) -> ScanResult:
        revision = revision or SourceRevision()
        result = ScanResult()
        self.parsed_manifests = {}

        changed_paths = self._changed_paths(revision)
        manifest_paths = manifests.find_manifests(changed_paths)
        if not manifest_paths:
            return result

        parsed_new: dict[str, ParsedManifest] = {}
        changes: list[PackageChange] = []

        for path in manifest_paths:
            if not manifests.is_parseable(path):
                result.degraded_reasons.append(
                    f"{path} is a lockfile format Sentinel-AI cannot parse yet; "
                    f"its dependencies were not checked"
                )
                continue

            new_content = self._read_new(path, revision)
            old_content = self._read_old(path, revision)
            if new_content is None:
                continue

            parsed = manifests.parse(path, new_content)
            if parsed is None:
                result.degraded_reasons.append(f"{path} could not be parsed")
                continue

            parsed_new[path] = parsed
            result.scanned_manifests.append(path)
            changes.extend(manifests.diff_manifests(path, old_content, new_content))

        self.parsed_manifests = parsed_new
        result.changes = _deduplicate(changes)
        if not result.changes and not parsed_new:
            return result

        result.findings.extend(
            heuristics.evaluate(result.changes, parsed_new, self.settings.policy)
        )

        trivy_findings, trivy_note = self._run_trivy(parsed_new, result.changes)
        result.findings.extend(trivy_findings)
        if trivy_note:
            result.trivy_available = False
            result.degraded_reasons.append(trivy_note)

        return result

    # ------------------------------------------------------------ revisions

    def _changed_paths(self, revision: SourceRevision) -> list[str]:
        if revision.mode == "range":
            return gitdiff.changed_files_between(
                self.root, revision.base_ref, revision.head_ref
            )
        if revision.mode == "worktree":
            return self._all_tracked_manifests()
        return gitdiff.staged_files(self.root)

    def _all_tracked_manifests(self) -> list[str]:
        """Every manifest on disk — used by `--all` for a full baseline scan."""
        found: list[str] = []
        for candidate in self.root.rglob("*"):
            if not candidate.is_file():
                continue
            relative = candidate.relative_to(self.root).as_posix()
            if manifests.identify(relative) is not None:
                found.append(relative)
        return found

    def _read_new(self, path: str, revision: SourceRevision) -> str | None:
        if revision.mode == "worktree":
            return gitdiff.read_worktree(self.root, path)
        if revision.mode == "range":
            return gitdiff.read_committed(self.root, path, revision.head_ref)
        return gitdiff.read_staged(self.root, path)

    def _read_old(self, path: str, revision: SourceRevision) -> str | None:
        if revision.mode == "worktree":
            # Nothing to compare against; treat every dependency as new.
            return None
        base = revision.base_ref if revision.mode == "range" else "HEAD"
        return gitdiff.read_committed(self.root, path, base)

    # ---------------------------------------------------------------- Trivy

    def _run_trivy(
        self,
        parsed: dict[str, ParsedManifest],
        changes: list[PackageChange],
    ) -> tuple[list[Finding], str | None]:
        """Scan staged manifests with Trivy. Returns (findings, degraded_note)."""
        config = self.settings.trivy
        if not config.enabled:
            return [], None

        binary = config.resolve_binary()
        if binary is None:
            return [], (
                f"Trivy not found (looked for `{config.binary_path}`); "
                f"CVE checking was skipped"
            )

        lockfiles = {
            path: manifest for path, manifest in parsed.items() if manifest.is_lockfile
        }
        # With no lockfile the CVE data would be guesswork, so fall back to
        # whatever manifests we do have and let Trivy decide what it can read.
        targets = lockfiles or parsed
        if not targets:
            return [], None

        with tempfile.TemporaryDirectory(prefix="sentinel-trivy-") as tmp:
            staging = Path(tmp)
            if not self._materialise(targets.keys(), staging):
                return [], "no manifest content could be staged for Trivy"

            command = [
                binary,
                "fs",
                "--scanners",
                "vuln",
                "--format",
                "json",
                "--quiet",
                "--exit-code",
                "0",
            ]
            if config.offline:
                command.append("--offline-scan")
            if config.skip_db_update:
                command.append("--skip-db-update")
            command.append(str(staging))

            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=config.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return [], (
                    f"Trivy exceeded {config.timeout_seconds:.0f}s and was "
                    f"cancelled; CVE results are incomplete"
                )
            except OSError as exc:
                return [], f"Trivy could not be executed: {exc}"

            if completed.returncode != 0:
                detail = (completed.stderr or "").strip().splitlines()
                tail = detail[-1] if detail else f"exit code {completed.returncode}"
                return [], f"Trivy failed ({tail}); CVE results are missing"

            try:
                report = json.loads(completed.stdout or "{}")
            except json.JSONDecodeError:
                return [], "Trivy returned output that could not be parsed"

        return _findings_from_trivy(report, changes), None

    def _materialise(self, paths, staging: Path) -> bool:
        """Write staged manifest content into a temp tree for Trivy to read.

        Trivy scans the filesystem, but the index is the source of truth here,
        so the staged blobs are copied out rather than pointing at the worktree.
        """
        wrote_any = False
        for path in paths:
            content = gitdiff.read_staged(self.root, path)
            if content is None:
                content = gitdiff.read_worktree(self.root, path)
            if content is None:
                continue
            target = staging / PurePosixPath(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            wrote_any = True
        return wrote_any


def _findings_from_trivy(report: dict, changes: list[PackageChange]) -> list[Finding]:
    """Convert Trivy's JSON into Findings, scoped to the packages that changed.

    Pre-existing vulnerabilities elsewhere in the tree are filtered out: this
    hook blocks what *this commit* introduces, and a wall of inherited CVEs
    would train developers to bypass it.
    """
    changed_names = {change.name.lower() for change in changes}
    by_name = {change.name.lower(): change for change in changes}
    findings: list[Finding] = []
    seen: set[tuple[str, str]] = set()

    for entry in report.get("Results") or []:
        for vulnerability in entry.get("Vulnerabilities") or []:
            package_name = str(vulnerability.get("PkgName", "")).strip()
            if not package_name:
                continue
            key_name = package_name.lower()
            if changed_names and key_name not in changed_names:
                continue

            vuln_id = str(vulnerability.get("VulnerabilityID", "UNKNOWN"))
            dedupe_key = (key_name, vuln_id)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            installed = str(vulnerability.get("InstalledVersion", "") or "?")
            fixed = str(vulnerability.get("FixedVersion", "") or "").strip()
            change = by_name.get(key_name)
            coordinate = change.coordinate if change else f"{package_name}@{installed}"

            findings.append(
                Finding(
                    source=FindingSource.TRIVY,
                    severity=Severity.parse(vulnerability.get("Severity")),
                    title=f"{vuln_id} in {package_name} {installed}",
                    detail=(
                        vulnerability.get("Title")
                        or vulnerability.get("Description")
                        or "No description supplied by the advisory."
                    ).strip()[:500],
                    package=coordinate,
                    remediation=(
                        f"Upgrade `{package_name}` to {fixed} or later."
                        if fixed
                        else "No fixed version is published yet; consider an alternative package."
                    ),
                    reference=vulnerability.get("PrimaryURL"),
                )
            )
    return findings


def _deduplicate(changes: list[PackageChange]) -> list[PackageChange]:
    """Collapse duplicates, preferring the direct declaration over a lockfile.

    A package added to both package.json and package-lock.json is one event to
    a developer, and should be reported once.
    """
    best: dict[tuple[str, str], PackageChange] = {}
    for change in changes:
        key = (change.ecosystem.value, change.name)
        existing = best.get(key)
        if existing is None or (change.is_direct and not existing.is_direct):
            best[key] = change
    return list(best.values())


def trivy_version(settings: Settings) -> str | None:
    """Installed Trivy version, or None. Used by `sentinel doctor`."""
    binary = settings.trivy.resolve_binary() or shutil.which("trivy")
    if binary is None:
        return None
    try:
        completed = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    first_line = (completed.stdout or "").strip().splitlines()
    return first_line[0] if first_line else None

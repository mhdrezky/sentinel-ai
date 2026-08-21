"""Shared data models passed between the scanner, AI, and decision layers.

Every module in Sentinel-AI speaks in these types so the pipeline stays
decoupled: the scanner never imports the AI client, the decision engine
never imports either of them.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Ecosystem(StrEnum):
    """Package registry a dependency comes from."""

    NPM = "npm"
    PYPI = "pypi"
    NUGET = "nuget"
    COMPOSER = "composer"

    @property
    def registry_name(self) -> str:
        return {
            Ecosystem.NPM: "npm",
            Ecosystem.PYPI: "PyPI",
            Ecosystem.NUGET: "NuGet",
            Ecosystem.COMPOSER: "Packagist",
        }[self]


class ChangeType(StrEnum):
    ADDED = "added"
    UPGRADED = "upgraded"
    DOWNGRADED = "downgraded"


class Severity(StrEnum):
    """Ordered severity. Compare with `Severity.rank`, not with `<`."""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return _SEVERITY_ORDER[self]

    @classmethod
    def parse(cls, raw: str | None) -> Severity:
        """Coerce a foreign severity string (Trivy, the LLM) into ours."""
        if not raw:
            return Severity.NONE
        normalised = raw.strip().lower()
        if normalised in ("unknown", "negligible", "info", "informational"):
            return Severity.LOW
        try:
            return cls(normalised)
        except ValueError:
            return Severity.MEDIUM


_SEVERITY_ORDER: dict[Severity, int] = {
    Severity.NONE: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


class FindingSource(StrEnum):
    """Which analysis stage produced a finding — shown in the report."""

    TRIVY = "trivy"
    HEURISTIC = "heuristic"
    POLICY = "policy"


class PackageChange(BaseModel):
    """A single dependency added or version-bumped in the staged diff."""

    name: str
    ecosystem: Ecosystem
    new_version: str | None = None
    old_version: str | None = None
    change_type: ChangeType = ChangeType.ADDED
    manifest_path: str
    is_direct: bool = True
    """False when the package only appears in a lockfile (a transitive dep)."""

    @property
    def coordinate(self) -> str:
        """Stable human-readable id, e.g. `npm:left-pad@1.3.0`."""
        version = self.new_version or "*"
        return f"{self.ecosystem.value}:{self.name}@{version}"


class Finding(BaseModel):
    """One reason a commit might be blocked."""

    source: FindingSource
    severity: Severity
    title: str
    detail: str
    package: str
    """The `coordinate` of the offending PackageChange."""
    remediation: str | None = None
    reference: str | None = None
    """CVE id, advisory URL, or similar."""


class ScanResult(BaseModel):
    """Everything the scanner learned, before any pass/fail decision."""

    changes: list[PackageChange] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    scanned_manifests: list[str] = Field(default_factory=list)
    trivy_available: bool = True
    degraded_reasons: list[str] = Field(default_factory=list)
    """Non-fatal problems (Trivy missing, AI unreachable) worth surfacing."""

    @property
    def highest_severity(self) -> Severity:
        if not self.findings:
            return Severity.NONE
        return max((f.severity for f in self.findings), key=lambda s: s.rank)


class Decision(BaseModel):
    """Final outcome. `exit_code` is what the Husky hook actually consumes."""

    blocked: bool
    exit_code: int
    reason: str
    findings: list[Finding] = Field(default_factory=list)
    """Only the findings that met or exceeded the blocking threshold."""
    warnings: list[Finding] = Field(default_factory=list)
    """Below-threshold findings — printed, but do not fail the commit."""

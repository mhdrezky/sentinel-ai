"""Turns evidence into a pass/fail answer.

This is the only module that decides anything. It takes the scanner's findings
plus the optional AI verdict and produces a `Decision` whose `exit_code` the
Husky hook consumes directly.

Exit codes:
    0  commit may proceed (possibly with warnings printed)
    1  commit blocked
    2  Sentinel-AI itself failed — see `main.py`
"""

from __future__ import annotations

from .config import PolicyConfig
from .models import Decision, Finding, ScanResult

EXIT_PASS = 0
EXIT_BLOCK = 1
EXIT_ERROR = 2

# Findings of these kinds block regardless of the configured severity floor,
# because each represents code execution or an unvetted source rather than a
# graded vulnerability.
_ALWAYS_BLOCK_TITLES = ("executes code at install time", "outside the registry")


def decide(scan: ScanResult, policy: PolicyConfig) -> Decision:
    """Turn scanner findings into a final outcome."""
    findings = list(scan.findings)
    findings = _drop_allowlisted(findings, policy)
    findings.sort(key=lambda f: f.severity.rank, reverse=True)

    threshold = policy.block_at_or_above.rank
    blocking: list[Finding] = []
    warnings: list[Finding] = []

    for finding in findings:
        if _is_blocking(finding, policy, threshold):
            blocking.append(finding)
        else:
            warnings.append(finding)

    if blocking:
        return Decision(
            blocked=True,
            exit_code=EXIT_BLOCK,
            reason=_block_reason(blocking),
            findings=blocking,
            warnings=warnings,
        )

    return Decision(
        blocked=False,
        exit_code=EXIT_PASS,
        reason=_pass_reason(scan, warnings),
        findings=[],
        warnings=warnings,
    )


def _is_blocking(finding: Finding, policy: PolicyConfig, threshold: int) -> bool:
    install_script, nonregistry = _ALWAYS_BLOCK_TITLES
    return (
        finding.severity.rank >= threshold
        or (policy.block_on_install_scripts and install_script in finding.title)
        or (policy.block_on_nonregistry_source and nonregistry in finding.title)
    )


def _drop_allowlisted(findings: list[Finding], policy: PolicyConfig) -> list[Finding]:
    return [
        finding
        for finding in findings
        if not policy.is_allowlisted(finding.package, finding.package)
    ]


def _block_reason(blocking: list[Finding]) -> str:
    packages = {finding.package for finding in blocking}
    top = blocking[0].severity.value.upper()
    if len(packages) == 1:
        return f"{top} risk in {next(iter(packages))}"
    return f"{len(blocking)} blocking findings across {len(packages)} packages ({top} highest)"


def _pass_reason(scan: ScanResult, warnings: list[Finding]) -> str:
    if not scan.changes:
        return "no dependency changes in this commit"
    count = len(scan.changes)
    noun = "dependency" if count == 1 else "dependencies"
    if warnings:
        return f"{count} {noun} checked, {len(warnings)} warning(s), nothing blocking"
    return f"{count} {noun} checked, no risks found"

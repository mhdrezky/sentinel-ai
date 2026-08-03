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
from .models import (
    AIVerdict,
    ChangeType,
    Decision,
    Finding,
    FindingSource,
    ScanResult,
    Severity,
)

EXIT_PASS = 0
EXIT_BLOCK = 1
EXIT_ERROR = 2

# Findings of these kinds block regardless of the configured severity floor,
# because each represents code execution or an unvetted source rather than a
# graded vulnerability.
_ALWAYS_BLOCK_TITLES = ("executes code at install time", "outside the registry")


def requires_ai_review(scan: ScanResult, policy: PolicyConfig) -> bool:
    """Whether the LLM call is worth its latency for this commit.

    The AI is the slowest stage by an order of magnitude, so it only runs when
    the deterministic layer found something ambiguous, or when a brand-new
    direct dependency appeared. A commit that only bumps a known-good package
    should never wait on inference.
    """
    if not scan.changes:
        return False
    if any(
        f.source in (FindingSource.HEURISTIC, FindingSource.TRIVY) for f in scan.findings
    ):
        return True
    # New direct dependencies are the primary attack vector, so they get a
    # look even when nothing deterministic fired.
    return any(
        change.is_direct and change.change_type is ChangeType.ADDED
        for change in scan.changes
    )


def decide(
    scan: ScanResult,
    verdict: AIVerdict | None,
    policy: PolicyConfig,
) -> Decision:
    """Combine scanner findings and the AI verdict into a final outcome."""
    findings = list(scan.findings)
    if verdict is not None:
        findings.extend(_findings_from_verdict(verdict, policy))

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


def _findings_from_verdict(verdict: AIVerdict, policy: PolicyConfig) -> list[Finding]:
    """Promote the model's judgement into findings the report can render.

    A low-confidence verdict is demoted one severity step: the model is an
    advisor here, and an uncertain opinion should not be the sole reason a
    developer's commit fails.
    """
    if verdict.risk_level is Severity.NONE and not verdict.packages:
        return []

    findings: list[Finding] = []
    # Tracked separately from `findings`: a verdict whose packages were all
    # allowlisted has been *answered*, and must not fall through to the
    # catch-all below and block on the same risk a second time.
    described_packages = False

    for package in verdict.packages:
        if package.risk_level is Severity.NONE:
            continue
        described_packages = True
        if policy.is_allowlisted(package.name, package.name):
            continue
        findings.append(
            Finding(
                source=FindingSource.AI,
                severity=_adjust_for_confidence(package.risk_level, verdict.confidence),
                title=f"AI review flagged {package.name}",
                detail=package.reason or verdict.summary,
                package=package.name,
                remediation=verdict.recommended_action or None,
            )
        )

    # Keep the overall verdict only when it says something the per-package
    # entries did not already cover.
    if verdict.risk_level is not Severity.NONE and not described_packages:
        findings.append(
            Finding(
                source=FindingSource.AI,
                severity=_adjust_for_confidence(verdict.risk_level, verdict.confidence),
                title="AI review flagged this dependency change",
                detail=_verdict_detail(verdict),
                package="(commit)",
                remediation=verdict.recommended_action or None,
            )
        )

    return findings


def _adjust_for_confidence(severity: Severity, confidence: float) -> Severity:
    if confidence >= 0.5 or severity is Severity.NONE:
        return severity
    order = [
        Severity.NONE,
        Severity.LOW,
        Severity.MEDIUM,
        Severity.HIGH,
        Severity.CRITICAL,
    ]
    return order[max(0, severity.rank - 1)]


def _verdict_detail(verdict: AIVerdict) -> str:
    parts = [verdict.summary.strip()] if verdict.summary.strip() else []
    parts.extend(f"- {indicator}" for indicator in verdict.indicators)
    return "\n".join(parts) or "The model reported elevated risk without detail."


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

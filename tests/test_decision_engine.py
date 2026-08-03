"""Pass/fail logic — the only module that decides a commit's fate."""

from __future__ import annotations

import pytest

from sentinel_ai.config import PolicyConfig
from sentinel_ai.decision_engine import (
    EXIT_BLOCK,
    EXIT_PASS,
    decide,
    requires_ai_review,
)
from sentinel_ai.models import (
    AIVerdict,
    ChangeType,
    Ecosystem,
    Finding,
    FindingSource,
    PackageChange,
    PackageVerdict,
    ScanResult,
    Severity,
)


def finding(
    severity: Severity,
    *,
    title: str = "Something",
    package: str = "npm:pkg@1.0.0",
    source: FindingSource = FindingSource.HEURISTIC,
) -> Finding:
    return Finding(
        source=source,
        severity=severity,
        title=title,
        detail="detail",
        package=package,
    )


def change(
    name: str = "pkg", *, direct: bool = True, added: bool = True
) -> PackageChange:
    return PackageChange(
        name=name,
        ecosystem=Ecosystem.NPM,
        new_version="1.0.0",
        change_type=ChangeType.ADDED if added else ChangeType.UPGRADED,
        manifest_path="package.json",
        is_direct=direct,
    )


class TestThreshold:
    @pytest.mark.parametrize(
        ("severity", "blocked"),
        [
            (Severity.CRITICAL, True),
            (Severity.HIGH, True),
            (Severity.MEDIUM, False),
            (Severity.LOW, False),
        ],
    )
    def test_default_threshold_is_high(self, severity, blocked):
        scan = ScanResult(changes=[change()], findings=[finding(severity)])
        decision = decide(scan, None, PolicyConfig())
        assert decision.blocked is blocked
        assert decision.exit_code == (EXIT_BLOCK if blocked else EXIT_PASS)

    def test_threshold_can_be_lowered(self):
        scan = ScanResult(changes=[change()], findings=[finding(Severity.MEDIUM)])
        policy = PolicyConfig(block_at_or_above=Severity.MEDIUM)
        assert decide(scan, None, policy).blocked is True

    def test_below_threshold_findings_become_warnings(self):
        scan = ScanResult(changes=[change()], findings=[finding(Severity.LOW)])
        decision = decide(scan, None, PolicyConfig())
        assert decision.warnings and not decision.findings

    def test_clean_scan_passes(self):
        decision = decide(ScanResult(changes=[change()]), None, PolicyConfig())
        assert decision.exit_code == EXIT_PASS
        assert "no risks found" in decision.reason


class TestAlwaysBlockRules:
    def test_install_script_blocks_below_threshold(self):
        scan = ScanResult(
            changes=[change()],
            findings=[finding(Severity.LOW, title="pkg executes code at install time")],
        )
        policy = PolicyConfig(block_at_or_above=Severity.CRITICAL)
        assert decide(scan, None, policy).blocked is True

    def test_rule_can_be_disabled(self):
        scan = ScanResult(
            changes=[change()],
            findings=[finding(Severity.LOW, title="pkg executes code at install time")],
        )
        policy = PolicyConfig(
            block_at_or_above=Severity.CRITICAL, block_on_install_scripts=False
        )
        assert decide(scan, None, policy).blocked is False


class TestAIVerdictHandling:
    def test_confident_high_risk_blocks(self):
        verdict = AIVerdict(
            risk_level=Severity.HIGH,
            confidence=0.9,
            summary="exfiltrates env vars",
            packages=[PackageVerdict(name="pkg", risk_level=Severity.HIGH, reason="bad")],
        )
        decision = decide(ScanResult(changes=[change()]), verdict, PolicyConfig())
        assert decision.blocked is True
        assert decision.findings[0].source is FindingSource.AI

    def test_low_confidence_is_demoted_one_step(self):
        # An uncertain model opinion should not be the sole reason to fail.
        verdict = AIVerdict(
            risk_level=Severity.HIGH,
            confidence=0.2,
            packages=[
                PackageVerdict(name="pkg", risk_level=Severity.HIGH, reason="maybe")
            ],
        )
        decision = decide(ScanResult(changes=[change()]), verdict, PolicyConfig())
        assert decision.blocked is False
        assert decision.warnings[0].severity is Severity.MEDIUM

    def test_clean_verdict_adds_nothing(self):
        verdict = AIVerdict(risk_level=Severity.NONE, confidence=0.95)
        decision = decide(ScanResult(changes=[change()]), verdict, PolicyConfig())
        assert decision.findings == [] and decision.warnings == []

    def test_allowlisted_package_survives_an_ai_flag(self):
        verdict = AIVerdict(
            risk_level=Severity.CRITICAL,
            confidence=1.0,
            packages=[PackageVerdict(name="internal-tool", risk_level=Severity.CRITICAL)],
        )
        policy = PolicyConfig(allowlist=["internal-tool"])
        assert decide(ScanResult(changes=[change()]), verdict, policy).blocked is False


class TestAITriageGate:
    def test_no_changes_means_no_inference(self):
        assert requires_ai_review(ScanResult(), PolicyConfig()) is False

    def test_new_direct_dependency_triggers_review(self):
        scan = ScanResult(changes=[change("brand-new")])
        assert requires_ai_review(scan, PolicyConfig()) is True

    def test_transitive_upgrade_alone_does_not(self):
        # Keeps the slow stage off the hot path for routine lockfile churn.
        scan = ScanResult(changes=[change("dep", direct=False, added=False)])
        assert requires_ai_review(scan, PolicyConfig()) is False

    def test_any_deterministic_finding_triggers_review(self):
        scan = ScanResult(
            changes=[change("dep", direct=False, added=False)],
            findings=[finding(Severity.MEDIUM)],
        )
        assert requires_ai_review(scan, PolicyConfig()) is True


class TestOrdering:
    def test_findings_are_reported_worst_first(self):
        scan = ScanResult(
            changes=[change()],
            findings=[
                finding(Severity.HIGH, title="high"),
                finding(Severity.CRITICAL, title="critical"),
            ],
        )
        decision = decide(scan, None, PolicyConfig())
        assert [f.title for f in decision.findings] == ["critical", "high"]

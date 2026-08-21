"""Pass/fail logic — the only module that decides a commit's fate."""

from __future__ import annotations

import pytest

from sentinel_ai.config import PolicyConfig
from sentinel_ai.decision_engine import (
    EXIT_BLOCK,
    EXIT_PASS,
    decide,
)
from sentinel_ai.models import (
    ChangeType,
    Ecosystem,
    Finding,
    FindingSource,
    PackageChange,
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
        decision = decide(scan, PolicyConfig())
        assert decision.blocked is blocked
        assert decision.exit_code == (EXIT_BLOCK if blocked else EXIT_PASS)

    def test_threshold_can_be_lowered(self):
        scan = ScanResult(changes=[change()], findings=[finding(Severity.MEDIUM)])
        policy = PolicyConfig(block_at_or_above=Severity.MEDIUM)
        assert decide(scan, policy).blocked is True

    def test_below_threshold_findings_become_warnings(self):
        scan = ScanResult(changes=[change()], findings=[finding(Severity.LOW)])
        decision = decide(scan, PolicyConfig())
        assert decision.warnings and not decision.findings

    def test_clean_scan_passes(self):
        decision = decide(ScanResult(changes=[change()]), PolicyConfig())
        assert decision.exit_code == EXIT_PASS
        assert "no risks found" in decision.reason


class TestAlwaysBlockRules:
    def test_install_script_blocks_below_threshold(self):
        scan = ScanResult(
            changes=[change()],
            findings=[finding(Severity.LOW, title="pkg executes code at install time")],
        )
        policy = PolicyConfig(block_at_or_above=Severity.CRITICAL)
        assert decide(scan, policy).blocked is True

    def test_rule_can_be_disabled(self):
        scan = ScanResult(
            changes=[change()],
            findings=[finding(Severity.LOW, title="pkg executes code at install time")],
        )
        policy = PolicyConfig(
            block_at_or_above=Severity.CRITICAL, block_on_install_scripts=False
        )
        assert decide(scan, policy).blocked is False


class TestOrdering:
    def test_findings_are_reported_worst_first(self):
        scan = ScanResult(
            changes=[change()],
            findings=[
                finding(Severity.HIGH, title="high"),
                finding(Severity.CRITICAL, title="critical"),
            ],
        )
        decision = decide(scan, PolicyConfig())
        assert [f.title for f in decision.findings] == ["critical", "high"]

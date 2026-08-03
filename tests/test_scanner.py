"""Scanner internals that do not require Trivy to be installed.

The subprocess call itself is not exercised here — see the note in the README
about verifying against a real Trivy binary. What *is* covered is the report
translation, which is where the scoping and de-duplication logic lives.
"""

from __future__ import annotations

from sentinel_ai.models import (
    ChangeType,
    Ecosystem,
    FindingSource,
    PackageChange,
    Severity,
)
from sentinel_ai.scanner import _deduplicate, _findings_from_trivy


def change(name: str, version: str = "1.0.0", *, direct: bool = True) -> PackageChange:
    return PackageChange(
        name=name,
        ecosystem=Ecosystem.NPM,
        new_version=version,
        manifest_path="package.json" if direct else "package-lock.json",
        is_direct=direct,
    )


def report(*vulnerabilities: dict) -> dict:
    return {
        "Results": [
            {"Target": "package-lock.json", "Vulnerabilities": list(vulnerabilities)}
        ]
    }


def vuln(
    package: str,
    vuln_id: str = "CVE-2024-0001",
    severity: str = "HIGH",
    fixed: str | None = "2.0.0",
) -> dict:
    entry = {
        "VulnerabilityID": vuln_id,
        "PkgName": package,
        "InstalledVersion": "1.0.0",
        "Severity": severity,
        "Title": "Prototype pollution",
        "PrimaryURL": f"https://avd.aquasec.com/nvd/{vuln_id.lower()}",
    }
    if fixed:
        entry["FixedVersion"] = fixed
    return entry


class TestTrivyTranslation:
    def test_vulnerability_becomes_a_finding(self):
        findings = _findings_from_trivy(report(vuln("axios")), [change("axios")])
        assert len(findings) == 1

        finding = findings[0]
        assert finding.source is FindingSource.TRIVY
        assert finding.severity is Severity.HIGH
        assert "CVE-2024-0001" in finding.title
        assert finding.package == "npm:axios@1.0.0"
        assert "2.0.0" in finding.remediation
        assert finding.reference.startswith("https://")

    def test_preexisting_vulnerabilities_are_filtered_out(self):
        """Only what this commit introduces is reported.

        A wall of inherited CVEs trains developers to reach for --no-verify.
        """
        findings = _findings_from_trivy(
            report(vuln("axios"), vuln("old-untouched-dep")), [change("axios")]
        )
        assert [f.package for f in findings] == ["npm:axios@1.0.0"]

    def test_package_name_matching_is_case_insensitive(self):
        findings = _findings_from_trivy(
            report(vuln("Newtonsoft.Json")), [change("newtonsoft.json")]
        )
        assert len(findings) == 1

    def test_duplicate_advisories_are_collapsed(self):
        # The same CVE can appear under several targets in one report.
        findings = _findings_from_trivy(
            report(vuln("axios"), vuln("axios")), [change("axios")]
        )
        assert len(findings) == 1

    def test_distinct_cves_on_one_package_are_kept(self):
        findings = _findings_from_trivy(
            report(vuln("axios", "CVE-2024-0001"), vuln("axios", "CVE-2024-0002")),
            [change("axios")],
        )
        assert len(findings) == 2

    def test_missing_fix_is_stated_rather_than_implied(self):
        findings = _findings_from_trivy(
            report(vuln("axios", fixed=None)), [change("axios")]
        )
        assert "No fixed version" in findings[0].remediation

    def test_unknown_severity_is_downgraded_not_dropped(self):
        findings = _findings_from_trivy(
            report(vuln("axios", severity="UNKNOWN")), [change("axios")]
        )
        assert findings[0].severity is Severity.LOW

    def test_empty_report_yields_nothing(self):
        assert _findings_from_trivy({}, [change("axios")]) == []
        assert _findings_from_trivy({"Results": None}, [change("axios")]) == []

    def test_entry_without_a_package_name_is_skipped(self):
        malformed = {"Results": [{"Vulnerabilities": [{"VulnerabilityID": "CVE-X"}]}]}
        assert _findings_from_trivy(malformed, [change("axios")]) == []


class TestDeduplicate:
    def test_direct_declaration_wins_over_lockfile_entry(self):
        """package.json and package-lock.json are one event to a developer."""
        collapsed = _deduplicate([change("axios", direct=False), change("axios")])
        assert len(collapsed) == 1
        assert collapsed[0].is_direct is True

    def test_order_does_not_matter(self):
        collapsed = _deduplicate([change("axios"), change("axios", direct=False)])
        assert len(collapsed) == 1
        assert collapsed[0].is_direct is True

    def test_same_name_in_different_ecosystems_is_kept_separate(self):
        npm = change("redis")
        pypi = PackageChange(
            name="redis",
            ecosystem=Ecosystem.PYPI,
            new_version="5.0.0",
            manifest_path="requirements.txt",
        )
        assert len(_deduplicate([npm, pypi])) == 2

    def test_upgrade_and_add_of_the_same_package_collapse(self):
        added = change("axios")
        upgraded = change("axios")
        upgraded.change_type = ChangeType.UPGRADED
        assert len(_deduplicate([added, upgraded])) == 1

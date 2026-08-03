"""Parser and diff behaviour — the layer that decides what gets scanned at all."""

from __future__ import annotations

import json

import pytest

from sentinel_ai import manifests
from sentinel_ai.models import ChangeType, Ecosystem


class TestIdentify:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("package.json", Ecosystem.NPM),
            ("apps/web/package-lock.json", Ecosystem.NPM),
            ("requirements.txt", Ecosystem.PYPI),
            ("requirements-dev.txt", Ecosystem.PYPI),
            ("pyproject.toml", Ecosystem.PYPI),
            ("src/Api/Api.csproj", Ecosystem.NUGET),
            ("packages.config", Ecosystem.NUGET),
            ("composer.json", Ecosystem.COMPOSER),
            ("README.md", None),
            ("src/app.component.ts", None),
        ],
    )
    def test_recognises_manifest_paths(self, path, expected):
        assert manifests.identify(path) is expected

    def test_vendored_manifests_are_excluded(self):
        paths = ["package.json", "node_modules/left-pad/package.json"]
        assert manifests.find_manifests(paths) == ["package.json"]


class TestPackageJson:
    def test_collects_all_dependency_blocks(self):
        content = json.dumps(
            {
                "dependencies": {"rxjs": "~7.8.0"},
                "devDependencies": {"typescript": "5.4.2"},
                "optionalDependencies": {"fsevents": "2.3.3"},
                "peerDependencies": {"react": "18.0.0"},
            }
        )
        parsed = manifests.parse("package.json", content)
        assert parsed is not None
        assert parsed.dependencies == {
            "rxjs": "~7.8.0",
            "typescript": "5.4.2",
            "fsevents": "2.3.3",
        }
        # peerDependencies declare a host contract, not an install.
        assert "react" not in parsed.dependencies

    def test_captures_lifecycle_scripts(self):
        content = json.dumps({"scripts": {"postinstall": "node setup.js"}})
        parsed = manifests.parse("package.json", content)
        assert parsed is not None
        assert parsed.scripts["postinstall"] == "node setup.js"

    def test_broken_json_returns_none_rather_than_raising(self):
        assert manifests.parse("package.json", '{"dependencies": ') is None


class TestPackageLock:
    def test_v3_lockfile_with_install_script_flag(self):
        content = json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {"name": "my-app", "version": "1.0.0"},
                    "node_modules/esbuild": {
                        "version": "0.20.0",
                        "hasInstallScript": True,
                    },
                    "node_modules/rxjs": {"version": "7.8.1"},
                },
            }
        )
        parsed = manifests.parse("package-lock.json", content)
        assert parsed is not None
        assert parsed.is_lockfile
        assert parsed.dependencies == {"esbuild": "0.20.0", "rxjs": "7.8.1"}
        assert parsed.install_script_packages == {"esbuild"}

    def test_v1_nested_lockfile_is_flattened(self):
        content = json.dumps(
            {
                "lockfileVersion": 1,
                "dependencies": {
                    "a": {"version": "1.0.0", "dependencies": {"b": {"version": "2.0.0"}}}
                },
            }
        )
        parsed = manifests.parse("package-lock.json", content)
        assert parsed is not None
        assert parsed.dependencies == {"a": "1.0.0", "b": "2.0.0"}


class TestPythonManifests:
    def test_requirements_ignores_comments_and_flags(self):
        content = (
            "# a comment\n"
            "requests==2.31.0\n"
            "flask>=2.0  # inline comment\n"
            "-r other.txt\n"
            "-e .\n"
            "\n"
            "pydantic[email]~=2.0\n"
        )
        parsed = manifests.parse("requirements.txt", content)
        assert parsed is not None
        assert set(parsed.dependencies) == {"requests", "flask", "pydantic"}
        assert parsed.dependencies["requests"] == "==2.31.0"

    def test_pyproject_reads_pep621_and_poetry(self):
        content = (
            "[project]\n"
            'dependencies = ["httpx>=0.28", "rich"]\n'
            "[tool.poetry.dependencies]\n"
            'python = "^3.13"\n'
            'click = "^8.1"\n'
        )
        parsed = manifests.parse("pyproject.toml", content)
        assert parsed is not None
        assert set(parsed.dependencies) == {"httpx", "rich", "click"}
        # "python" is an interpreter constraint, not a package.
        assert "python" not in parsed.dependencies


class TestNuGetAndComposer:
    def test_csproj_reads_attribute_and_child_versions(self):
        content = """<Project Sdk="Microsoft.NET.Sdk">
          <ItemGroup>
            <PackageReference Include="Serilog" Version="3.1.1" />
            <PackageReference Include="Dapper">
              <Version>2.1.35</Version>
            </PackageReference>
          </ItemGroup>
        </Project>"""
        parsed = manifests.parse("Api.csproj", content)
        assert parsed is not None
        assert parsed.dependencies == {"Serilog": "3.1.1", "Dapper": "2.1.35"}

    def test_composer_json_skips_platform_constraints(self):
        content = json.dumps(
            {
                "require": {"php": ">=8.1", "ext-json": "*", "monolog/monolog": "^3.0"},
                "require-dev": {"phpunit/phpunit": "^10.0"},
            }
        )
        parsed = manifests.parse("composer.json", content)
        assert parsed is not None
        assert parsed.dependencies == {
            "monolog/monolog": "^3.0",
            "phpunit/phpunit": "^10.0",
        }


class TestDiffManifests:
    def _pkg(self, deps: dict[str, str]) -> str:
        return json.dumps({"dependencies": deps})

    def test_new_manifest_marks_everything_added(self):
        changes = manifests.diff_manifests(
            "package.json", None, self._pkg({"rxjs": "7.8.1"})
        )
        assert len(changes) == 1
        assert changes[0].change_type is ChangeType.ADDED
        assert changes[0].old_version is None

    def test_unchanged_dependencies_are_ignored(self):
        same = self._pkg({"rxjs": "7.8.1"})
        assert manifests.diff_manifests("package.json", same, same) == []

    def test_version_bump_is_reported_as_upgrade(self):
        changes = manifests.diff_manifests(
            "package.json",
            self._pkg({"rxjs": "7.8.0"}),
            self._pkg({"rxjs": "7.8.1"}),
        )
        assert len(changes) == 1
        assert changes[0].change_type is ChangeType.UPGRADED
        assert changes[0].old_version == "7.8.0"

    def test_downgrade_is_distinguished(self):
        changes = manifests.diff_manifests(
            "package.json",
            self._pkg({"rxjs": "7.8.1"}),
            self._pkg({"rxjs": "7.2.0"}),
        )
        assert changes[0].change_type is ChangeType.DOWNGRADED

    def test_removals_are_not_reported(self):
        changes = manifests.diff_manifests(
            "package.json",
            self._pkg({"rxjs": "7.8.1", "lodash": "4.17.21"}),
            self._pkg({"rxjs": "7.8.1"}),
        )
        assert changes == []

    def test_coordinate_format(self):
        changes = manifests.diff_manifests(
            "package.json", None, self._pkg({"left-pad": "1.3.0"})
        )
        assert changes[0].coordinate == "npm:left-pad@1.3.0"


class TestUnparsedLockfiles:
    def test_yarn_lock_is_identified_but_not_parseable(self):
        assert manifests.identify("yarn.lock") is Ecosystem.NPM
        assert not manifests.is_parseable("yarn.lock")
        assert manifests.parse("yarn.lock", "whatever") is None

"""The machine-wide hook.

Every test here runs against a sandboxed global git config (`GIT_CONFIG_GLOBAL`)
and a redirected `~/.sentinel-ai`, so nothing touches the developer's own git
setup — which is exactly what this feature changes when it runs for real.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sentinel_ai import globalhook
from sentinel_ai.config import Settings
from sentinel_ai.decision_engine import EXIT_ERROR, EXIT_PASS
from sentinel_ai.globalhook import (
    HOOKS_PATH_KEY,
    GlobalHookError,
    install,
    parse_organizations,
    render_hook,
    status,
    uninstall,
)
from sentinel_ai.main import main

# Stand-ins: this repository is public, so no real organisation names here.
ORGS = ["acme-corp", "acme-labs"]


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway machine: its own global git config and host data dir."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setattr("sentinel_ai.config.host_data_dir", lambda: home / ".sentinel-ai")
    return home


def git_global(key: str) -> str | None:
    result = subprocess.run(
        ["git", "config", "--global", "--get", key],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or None


class TestRenderHook:
    def test_guard_precedes_the_cli(self):
        """The whole point: an unrelated repo must not start Python at all."""
        script = render_hook(ORGS)
        assert script.index("remote.origin.url") < script.index("sentinel-ai check")

    def test_every_organisation_becomes_a_pattern(self):
        script = render_hook(ORGS)
        assert "*acme-corp*|*acme-labs*)" in script

    def test_all_repos_has_no_guard(self):
        script = render_hook([])
        assert "remote.origin.url" not in script
        assert "exec sentinel-ai check" in script

    def test_organisations_round_trip(self):
        assert parse_organizations(render_hook(ORGS)) == ORGS
        assert parse_organizations(render_hook([])) == []
        assert parse_organizations("#!/bin/sh\nexit 0\n") is None


class TestGuardBehaviour:
    """Run the generated guard for real — a guard that never fires is worse
    than none, and a guard that always fires blocks personal work."""

    def _run_guard(self, repo: Path, script: str) -> int:
        # Everything above the `exec` line, so the test observes the decision
        # without needing the CLI on PATH.
        guard = script.split("exec sentinel-ai check")[0]
        return subprocess.run(
            ["sh", "-c", guard + "\nexit 42"], cwd=repo, check=False
        ).returncode

    @pytest.fixture
    def repo(self, tmp_path: Path) -> Path:
        root = tmp_path / "repo"
        root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        return root

    def test_company_remote_reaches_the_cli(self, repo: Path):
        subprocess.run(
            [
                "git",
                "remote",
                "add",
                "origin",
                "git@bitbucket.org:acme-corp/service.git",
            ],
            cwd=repo,
            check=True,
        )
        assert self._run_guard(repo, render_hook(ORGS)) == 42

    def test_personal_remote_exits_early(self, repo: Path):
        subprocess.run(
            ["git", "remote", "add", "origin", "git@github.com:someone/hobby.git"],
            cwd=repo,
            check=True,
        )
        assert self._run_guard(repo, render_hook(ORGS)) == 0

    def test_repo_without_a_remote_is_skipped(self, repo: Path):
        """No origin means nothing to match; local scratch repos stay quiet."""
        assert self._run_guard(repo, render_hook(ORGS)) == 0

    def test_all_repos_never_exits_early(self, repo: Path):
        assert self._run_guard(repo, render_hook([])) == 42


class TestInstall:
    def test_writes_the_script_and_points_git_at_it(self, machine: Path):
        path = install(ORGS)
        assert path.is_file()
        assert git_global(HOOKS_PATH_KEY) == globalhook.hooks_dir().as_posix()

    def test_hook_is_executable(self, machine: Path):
        """Git checks the executable bit before it will run a hook."""
        import os

        path = install(ORGS)
        assert os.access(path, os.X_OK)

    def test_refuses_to_clobber_another_hooks_path(self, machine: Path):
        subprocess.run(
            ["git", "config", "--global", HOOKS_PATH_KEY, "/somewhere/else"],
            check=True,
        )
        with pytest.raises(GlobalHookError, match="already set"):
            install(ORGS)
        assert git_global(HOOKS_PATH_KEY) == "/somewhere/else"

    def test_force_replaces_it(self, machine: Path):
        subprocess.run(
            ["git", "config", "--global", HOOKS_PATH_KEY, "/somewhere/else"],
            check=True,
        )
        install(ORGS, force=True)
        assert git_global(HOOKS_PATH_KEY) == globalhook.hooks_dir().as_posix()

    def test_reinstall_is_not_a_conflict(self, machine: Path):
        install(ORGS)
        install(["acme-corp"])
        assert parse_organizations(
            globalhook.hook_path().read_text(encoding="utf-8")
        ) == ["acme-corp"]


class TestStatusAndDrift:
    def test_reports_installed_scope(self, machine: Path):
        install(ORGS)
        state = status()
        assert state.installed
        assert state.installed_organizations == ORGS

    def test_foreign_hooks_path_is_reported_not_claimed(self, machine: Path):
        subprocess.run(
            ["git", "config", "--global", HOOKS_PATH_KEY, "/elsewhere"], check=True
        )
        state = status()
        assert state.installed is False
        assert state.points_elsewhere is True


class TestUninstall:
    def test_removes_script_and_config(self, machine: Path):
        install(ORGS)
        removed = uninstall()
        assert len(removed) == 2
        assert git_global(HOOKS_PATH_KEY) is None
        assert not globalhook.hook_path().exists()

    def test_leaves_someone_elses_hooks_path_alone(self, machine: Path):
        subprocess.run(
            ["git", "config", "--global", HOOKS_PATH_KEY, "/elsewhere"], check=True
        )
        uninstall()
        assert git_global(HOOKS_PATH_KEY) == "/elsewhere"

    def test_uninstalling_nothing_is_not_an_error(self, machine: Path):
        assert uninstall() == []


class TestCLI:
    def test_no_configured_organisations_refuses_rather_than_covering_everything(
        self, machine: Path
    ):
        """The bundled config ships empty, so the unconfigured case is the
        common one — and defaulting it to "every repo" would quietly put the
        hook on personal projects."""
        assert Settings.load().hook.organizations == []
        assert main(["install-global-hook"]) == EXIT_ERROR
        assert not globalhook.hook_path().exists()

    def test_uses_configured_organisations_when_present(self, machine: Path, monkeypatch):
        settings = Settings()
        settings.hook.organizations = ["acme-corp"]
        monkeypatch.setattr(Settings, "load", classmethod(lambda cls: settings))

        assert main(["install-global-hook"]) == EXIT_PASS
        script = globalhook.hook_path().read_text(encoding="utf-8")
        assert parse_organizations(script) == ["acme-corp"]

    def test_org_flag_overrides_config(self, machine: Path):
        assert main(["install-global-hook", "--org", "acme"]) == EXIT_PASS
        script = globalhook.hook_path().read_text(encoding="utf-8")
        assert parse_organizations(script) == ["acme"]

    def test_all_and_org_together_is_rejected(self, machine: Path):
        assert main(["install-global-hook", "--all", "--org", "acme"]) == EXIT_ERROR
        assert not globalhook.hook_path().exists()

    def test_all_installs_without_a_guard(self, machine: Path):
        assert main(["install-global-hook", "--all"]) == EXIT_PASS
        assert (
            parse_organizations(globalhook.hook_path().read_text(encoding="utf-8")) == []
        )

    def test_uninstall_command(self, machine: Path):
        main(["install-global-hook"])
        assert main(["uninstall-global-hook"]) == EXIT_PASS
        assert git_global(HOOKS_PATH_KEY) is None


class TestDriftOnlyWhenConfigDrove:
    """The documented rollout command passes `--org`, and the shipped config is
    empty — so comparing the two produced a warning on every clean install, and
    its advice ("re-run without the flag") would have failed outright."""

    def test_flag_install_never_reports_drift(self, machine: Path):
        install(ORGS, from_config=False)
        state = status()
        assert state.installed_organizations == ORGS
        assert state.drifted_from([]) is False
        assert state.drifted_from(["something-else"]) is False

    def test_config_install_still_reports_drift(self, machine: Path):
        install(ORGS, from_config=True)
        assert status().drifted_from(ORGS) is False
        assert status().drifted_from(["something-else"]) is True

    def test_script_without_the_marker_reports_no_drift(self, machine: Path):
        """0.3.0 and 0.3.1 wrote no source marker; silence beats a wrong warning."""
        install(ORGS, from_config=True)
        path = globalhook.hook_path()
        stripped = "\n".join(
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.startswith("# sentinel-ai:source=")
        )
        path.write_text(stripped, encoding="utf-8", newline="\n")

        assert status().drifted_from(["something-else"]) is False

    def test_cli_org_flag_marks_the_install_as_flag_driven(self, machine: Path):
        assert main(["install-global-hook", "--org", "acme-corp"]) == EXIT_PASS
        assert status().installed_from_config is False
        assert status().drifted_from([]) is False

    def test_cli_without_flags_marks_it_config_driven(self, machine: Path, monkeypatch):
        settings = Settings()
        settings.hook.organizations = ["acme-corp"]
        monkeypatch.setattr(Settings, "load", classmethod(lambda cls: settings))

        assert main(["install-global-hook"]) == EXIT_PASS
        assert status().installed_from_config is True
        assert status().drifted_from(["moved-on"]) is True

"""Tests for `sentinel-ai update`."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sentinel_ai.decision_engine import EXIT_ERROR, EXIT_PASS
from sentinel_ai.main import main
from sentinel_ai.update import UpdateError, fetch_latest_release_tag, run_update


class TestFetchLatestReleaseTag:
    def test_parses_tag_name(self):
        payload = json.dumps({"tag_name": "v1.2.3"}).encode()
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = payload

        with patch("sentinel_ai.update.urlopen", return_value=response):
            assert fetch_latest_release_tag() == "v1.2.3"

    def test_missing_tag_raises(self):
        payload = json.dumps({"name": "Release"}).encode()
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = payload

        with (
            patch("sentinel_ai.update.urlopen", return_value=response),
            pytest.raises(UpdateError, match="no tag_name"),
        ):
            fetch_latest_release_tag()


class TestRunUpdate:
    def test_installs_latest_release(self, tmp_path: Path):
        with (
            patch("sentinel_ai.update.fetch_latest_release_tag", return_value="v9.9.9"),
            patch("sentinel_ai.update._uv_tool_install") as install,
        ):
            ref = run_update()
        assert ref == "v9.9.9"
        install.assert_called_once_with(
            ["git+https://github.com/mhdrezky/sentinel-ai.git@v9.9.9"]
        )

    def test_installs_local_source(self, tmp_path: Path):
        with patch("sentinel_ai.update._uv_tool_install") as install:
            ref = run_update(source=tmp_path)
        assert ref == str(tmp_path.resolve())
        install.assert_called_once_with(
            ["--from", str(tmp_path.resolve()), "sentinel-ai"]
        )

    def test_missing_source_raises(self, tmp_path: Path):
        missing = tmp_path / "missing"
        with pytest.raises(UpdateError, match="source path not found"):
            run_update(source=missing)

    def test_uv_failure_surfaces_stderr(self, tmp_path: Path):
        completed = MagicMock(returncode=1, stdout="", stderr="network error")
        with (
            patch("sentinel_ai.update.shutil.which", return_value="/usr/bin/uv"),
            patch("sentinel_ai.update.subprocess.run", return_value=completed),
            pytest.raises(UpdateError, match="network error"),
        ):
            run_update(source=tmp_path)


class TestUpdateCommand:
    def test_success(self):
        with patch("sentinel_ai.update.run_update", return_value="v1.0.0"):
            assert main(["update", "--no-color"]) == EXIT_PASS

    def test_failure(self):
        with patch(
            "sentinel_ai.update.run_update",
            side_effect=UpdateError("uv tool install failed"),
        ):
            assert main(["update", "--no-color"]) == EXIT_ERROR


class TestSelfReplacementGuard:
    """Windows cannot replace the environment it is running from.

    uv deletes `Lib/site-packages` before it hits the lock on `Scripts`, so a
    failed self-update leaves no CLI at all rather than the previous version.
    That happened twice on a real machine before the guard existed.
    """

    def _tool_env(self, tmp_path: Path, monkeypatch) -> Path:
        (tmp_path / "uv-receipt.toml").write_text("[tool]\n", encoding="utf-8")
        monkeypatch.setattr("sentinel_ai.update.sys.prefix", str(tmp_path))
        return tmp_path

    def test_detects_a_uv_tool_environment(self, tmp_path: Path, monkeypatch):
        from sentinel_ai.update import running_from_uv_tool_env

        monkeypatch.setattr("sentinel_ai.update.sys.prefix", str(tmp_path))
        assert running_from_uv_tool_env() is None

        self._tool_env(tmp_path, monkeypatch)
        assert running_from_uv_tool_env() == tmp_path

    def test_windows_refuses_and_names_the_way_out(self, tmp_path: Path, monkeypatch):
        self._tool_env(tmp_path, monkeypatch)
        monkeypatch.setattr("sentinel_ai.update.sys.platform", "win32")

        # Nothing is patched out: the refusal has to happen before the release
        # lookup, or a rate-limited API turns it into an unrelated error.
        with (
            patch("sentinel_ai.update.urlopen", side_effect=AssertionError("network")),
            pytest.raises(UpdateError) as excinfo,
        ):
            run_update(source=None)

        message = str(excinfo.value)
        assert "uv tool install --force" in message
        assert "install.ps1" in message

    def test_posix_still_self_updates(self, tmp_path: Path, monkeypatch):
        """Replacing a running process's files is fine outside Windows."""
        self._tool_env(tmp_path, monkeypatch)
        monkeypatch.setattr("sentinel_ai.update.sys.platform", "linux")

        with (
            patch("sentinel_ai.update.fetch_latest_release_tag", return_value="v9.9.9"),
            patch("sentinel_ai.update.shutil.which", return_value="/usr/bin/uv"),
            patch("sentinel_ai.update.subprocess.run") as run,
        ):
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            assert run_update() == "v9.9.9"

    def test_outside_a_tool_env_windows_is_fine(self, tmp_path: Path, monkeypatch):
        """A source checkout run via `uv run` is not the environment at risk."""
        monkeypatch.setattr("sentinel_ai.update.sys.prefix", str(tmp_path))
        monkeypatch.setattr("sentinel_ai.update.sys.platform", "win32")

        with (
            patch("sentinel_ai.update.fetch_latest_release_tag", return_value="v9.9.9"),
            patch("sentinel_ai.update.shutil.which", return_value="uv"),
            patch("sentinel_ai.update.subprocess.run") as run,
        ):
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            assert run_update() == "v9.9.9"

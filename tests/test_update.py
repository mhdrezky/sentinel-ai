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
        install.assert_called_once_with(["--from", str(tmp_path.resolve()), "sentinel-ai"])

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

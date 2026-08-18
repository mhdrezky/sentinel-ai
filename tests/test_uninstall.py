"""Tests for `sentinel-ai uninstall`."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sentinel_ai.decision_engine import EXIT_ERROR, EXIT_PASS
from sentinel_ai.main import main
from sentinel_ai.uninstall import UninstallError, run_uninstall


@pytest.fixture
def host_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / ".sentinel-ai"
    data_dir.mkdir()
    (data_dir / "config.toml").write_text("[policy]\n", encoding="utf-8")
    (data_dir / "bin").mkdir()
    (data_dir / "bin" / "trivy").write_text("", encoding="utf-8")
    monkeypatch.setattr("sentinel_ai.uninstall.host_data_dir", lambda: data_dir)
    monkeypatch.setattr("sentinel_ai.config.host_data_dir", lambda: data_dir)
    return data_dir


class TestRunUninstall:
    def test_requires_yes(self):
        with pytest.raises(UninstallError, match="--yes"):
            run_uninstall(yes=False)

    def test_removes_host_data_and_uv_tool(self, host_dir: Path):
        completed = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("sentinel_ai.uninstall._uv_binary", return_value="/usr/bin/uv"),
            patch("sentinel_ai.uninstall.subprocess.run", return_value=completed) as run,
        ):
            removed = run_uninstall(yes=True)

        assert not host_dir.exists()
        assert str(host_dir) in removed
        assert "uv tool: sentinel-ai" in removed
        run.assert_called_once_with(
            ["/usr/bin/uv", "tool", "uninstall", "sentinel-ai"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def test_uv_not_installed_is_ok(self, host_dir: Path):
        completed = MagicMock(
            returncode=2,
            stdout="",
            stderr="error: `sentinel-ai` is not installed",
        )
        with (
            patch("sentinel_ai.uninstall._uv_binary", return_value="/usr/bin/uv"),
            patch("sentinel_ai.uninstall.subprocess.run", return_value=completed),
        ):
            removed = run_uninstall(yes=True)

        assert str(host_dir) in removed
        assert "uv tool: sentinel-ai" not in removed

    def test_missing_host_dir_still_uninstalls_tool(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        data_dir = tmp_path / ".sentinel-ai"
        monkeypatch.setattr("sentinel_ai.uninstall.host_data_dir", lambda: data_dir)
        completed = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("sentinel_ai.uninstall._uv_binary", return_value="/usr/bin/uv"),
            patch("sentinel_ai.uninstall.subprocess.run", return_value=completed),
        ):
            removed = run_uninstall(yes=True)

        assert removed == ["uv tool: sentinel-ai"]


class TestUninstallCommand:
    def test_without_yes_lists_targets(self, host_dir: Path):
        assert main(["uninstall", "--no-color"]) == EXIT_ERROR

    def test_with_yes_removes_everything(self, host_dir: Path):
        completed = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("sentinel_ai.uninstall._uv_binary", return_value="/usr/bin/uv"),
            patch("sentinel_ai.uninstall.subprocess.run", return_value=completed),
        ):
            assert main(["uninstall", "--yes", "--no-color"]) == EXIT_PASS
        assert not host_dir.exists()

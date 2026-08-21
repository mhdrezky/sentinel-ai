from __future__ import annotations

from pathlib import Path

import pytest

from sentinel_ai.config import (
    Settings,
    ensure_host_config,
    host_config_path,
    open_host_config_in_editor,
    resolved_config_paths,
)
from sentinel_ai.main import main


@pytest.fixture
def no_host_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Ignore the developer's real ~/.sentinel-ai/config.toml during tests."""
    missing = tmp_path / "missing-config.toml"
    monkeypatch.setattr("sentinel_ai.config.host_config_path", lambda: missing)
    monkeypatch.delenv("SENTINEL_CONFIG", raising=False)
    monkeypatch.delenv("SENTINEL_AI_BASE_URL", raising=False)
    monkeypatch.delenv("SENTINEL_AI_MODEL", raising=False)


def test_bundled_defaults(no_host_config: None) -> None:
    settings = Settings.load()

    assert settings.ai.base_url == "http://localhost:8000/v1"
    assert settings.ai.model == "local-model"
    assert settings.diff_review.max_output_tokens == 256


def test_global_config_overrides_bundled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_host_config: None
) -> None:
    global_config = tmp_path / "override.toml"
    global_config.write_text(
        '[policy]\nblock_at_or_above = "critical"\n'
        '[ai]\nbase_url = "http://override/v1"\nmodel = "override-model"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("SENTINEL_CONFIG", str(global_config))

    settings = Settings.load()

    assert settings.policy.block_at_or_above.value == "critical"
    assert settings.ai.base_url == "http://override/v1"
    assert settings.ai.model == "override-model"


def test_host_config_overrides_bundled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_host_config: None
) -> None:
    host_config = tmp_path / ".sentinel-ai" / "config.toml"
    host_config.parent.mkdir(parents=True)
    host_config.write_text(
        '[ai]\nbase_url = "http://host-override/v1"\nmodel = "host-model"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("sentinel_ai.config.host_config_path", lambda: host_config)

    settings = Settings.load()

    assert settings.ai.base_url == "http://host-override/v1"
    assert settings.ai.model == "host-model"


def test_host_config_with_utf8_bom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_host_config: None
) -> None:
    """install.ps1 on Windows PowerShell 5.1 used to write a BOM; still parse it."""
    host_config = tmp_path / ".sentinel-ai" / "config.toml"
    host_config.parent.mkdir(parents=True)
    host_config.write_text(
        '[ai]\nbase_url = "http://bom-host/v1"\n',
        encoding="utf-8-sig",
    )
    monkeypatch.setattr("sentinel_ai.config.host_config_path", lambda: host_config)

    settings = Settings.load()

    assert settings.ai.base_url == "http://bom-host/v1"


def test_env_overrides_global_ai_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_host_config: None
) -> None:
    global_config = tmp_path / "override.toml"
    global_config.write_text(
        '[ai]\nbase_url = "http://global/v1"\nmodel = "global-model"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("SENTINEL_CONFIG", str(global_config))
    monkeypatch.setenv("SENTINEL_AI_BASE_URL", "http://env/v1")
    monkeypatch.setenv("SENTINEL_AI_MODEL", "env-model")

    settings = Settings.load()

    assert settings.ai.base_url == "http://env/v1"
    assert settings.ai.model == "env-model"


def test_resolved_config_paths_includes_existing_files(no_host_config: None) -> None:
    paths = resolved_config_paths()
    assert paths
    assert any(path.name == "sentinel.toml" for path in paths)


def test_host_config_path_location() -> None:
    path = host_config_path()
    assert path.name == "config.toml"
    assert path.parent.name == ".sentinel-ai"


def test_config_command_json(
    capsys: pytest.CaptureFixture[str], no_host_config: None
) -> None:
    exit_code = main(["config", "--json"])
    assert exit_code == 0
    payload = __import__("json").loads(capsys.readouterr().out)
    assert payload["ai"]["base_url"] == "http://localhost:8000/v1"
    assert payload["sources"]


def test_ensure_host_config_creates_from_bundled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_host_config: None
) -> None:
    host_config = tmp_path / ".sentinel-ai" / "config.toml"
    monkeypatch.setattr("sentinel_ai.config.host_config_path", lambda: host_config)

    created = ensure_host_config()

    assert created == host_config
    assert host_config.is_file()
    assert "[policy]" in host_config.read_text(encoding="utf-8")


def test_config_edit_opens_editor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_host_config: None
) -> None:
    host_config = tmp_path / ".sentinel-ai" / "config.toml"
    monkeypatch.setattr("sentinel_ai.config.host_config_path", lambda: host_config)
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> None:
        calls.append(command)

    monkeypatch.setattr("sentinel_ai.config.subprocess.run", fake_run)
    monkeypatch.setattr("sentinel_ai.config.sys.platform", "win32")

    assert main(["config", "edit", "--no-color"]) == 0
    assert calls == [["notepad.exe", str(host_config)]]


def test_open_host_config_in_editor_uses_editor_on_linux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_host_config: None
) -> None:
    host_config = tmp_path / ".sentinel-ai" / "config.toml"
    monkeypatch.setattr("sentinel_ai.config.host_config_path", lambda: host_config)
    monkeypatch.setenv("EDITOR", "vim")
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> None:
        calls.append(command)

    monkeypatch.setattr("sentinel_ai.config.subprocess.run", fake_run)
    monkeypatch.setattr("sentinel_ai.config.sys.platform", "linux")

    path = open_host_config_in_editor()

    assert path == host_config
    assert calls == [["vim", str(host_config)]]

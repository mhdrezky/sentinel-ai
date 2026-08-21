"""Keep the test suite off the machine it runs on.

Sentinel-AI reaches outside the repository by design: `~/.sentinel-ai` holds the
host config, the Trivy binary and the machine-wide hook, and `install-global-hook`
writes to the user's *global* git config. A test that forgets to redirect one of
those does not fail — it quietly edits the developer's real setup.

That is not hypothetical. `run_uninstall` learned to clear the global hook, and
the uninstall tests, which had no idea `globalhook` existed, deleted a working
hook off a developer's machine twice in one session. Commits went unguarded
afterwards with nothing to indicate why.

So the redirect is autouse and applies to every test, rather than something each
test has to remember. Tests that need their own location still override these —
`monkeypatch` inside a test wins over a fixture that already ran.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_host_environment(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Point host data and global git config at throwaway locations."""
    home = tmp_path_factory.mktemp("host")
    # Keeps the real shape — code and tests alike expect the directory to be
    # named `.sentinel-ai` — while pointing somewhere disposable. Left
    # uncreated, like the real one before a first install.
    sandbox = home / ".sentinel-ai"

    # Every caller resolves this through the module, so one patch covers
    # config, globalhook and uninstall alike.
    monkeypatch.setattr("sentinel_ai.config.host_data_dir", lambda: sandbox)

    # Honoured by git 2.32+; without it `git config --global` edits the real one.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(home / "gitconfig-system"))

    return sandbox

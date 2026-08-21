# CLAUDE.md — Sentinel-AI

Context for Claude Code sessions in this repository.

**[`AGENTS.md`](AGENTS.md) is canonical** for roles, review workflow, config layering, and
code rules. Read it before making changes. This file covers what Claude needs most often
and does not repeat what is already there.

## What this tool does

Sentinel-AI runs on the commit boundary and does two orthogonal things:

```
staged index ─┬─ manifest diff → heuristics + trivy → exit 0|1
              └─ code diff → grounded AI review (network, watermark)
```

Both run inside `sentinel-ai check`, the single line the `pre-commit` hook calls. There
is no second hook and no per-repo install step — a feature reaches a machine when the CLI
is updated. Keep it that way when adding layers.

**AI means the diff reviewer, and only that.** The dependency side is deterministic:
heuristics plus Trivy. An earlier AI stage that judged dependency changes was removed —
do not reintroduce a model call on that path.

The hook itself reaches a machine through `install-global-hook` — see below.

## How the hook gets onto a machine

Git never installs hooks on clone: cloning a repository must not be able to run its code.
So a hook is per-working-copy, and with a dozen repositories across ten machines that is a
hundred acts of remembering. It did not happen — the CLI ended up on every machine while
the gate ran in one repository out of six.

`sentinel-ai install-global-hook` sets git's global `core.hooksPath` to
`~/.sentinel-ai/hooks`, moving that to once per machine. The generated script filters by
`remote.origin.url` so it stays out of personal projects, and `uninstall-global-hook`
reverses it.

Husky still works where it is already set up: a repository-local `core.hooksPath` wins
over the global one. `install-hook` (the Husky path) remains for those repositories.

## Verify before you claim done

```powershell
uv run python -m pytest
uv run ruff check .
uv run ruff format --check .
```

All three must pass. `ruff format --check` is not optional: the release workflow gates on
it, so leaving unrelated files unformatted blocks the next release rather than the next
commit. There is **no CI on push to `main`** — only on tags — so lint rot accumulates
invisibly until someone tries to ship.

Integration tests that need a live model server are marked `integration` and deselected by
default. Run them with a server configured:

```powershell
$env:SENTINEL_AI_BASE_URL = "http://<host>:<port>/v1"
uv run python -m pytest tests/test_diff_review_corpus.py -m integration
```

## Releasing

A release is a **tag push**. Nothing is created by hand in the GitHub UI, and there are no
PyInstaller artifacts — that pattern was abandoned after `v0.1.0`.

### 1. Bump the version in three places

```
pyproject.toml               version = "X.Y.Z"
src/sentinel_ai/__init__.py  __version__ = "X.Y.Z"
uv.lock                      regenerate — it records the project version
```

`uv.lock` is the one that gets forgotten. Run `uv sync --group dev` after editing the other
two and commit the resulting lock change, or the tagged tree is internally inconsistent.

### 2. Run the workflow's own gate locally

```powershell
uv sync --group dev
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

These are the exact commands in `.github/workflows/release.yml`. A failure here means a
failed release, so check before tagging rather than after.

### 3. Commit, push, tag, push the tag

```powershell
git push origin main
git tag -a vX.Y.Z -m "vX.Y.Z"
git push origin vX.Y.Z
```

### 4. Confirm it published

```bash
curl -s https://api.github.com/repos/mhdrezky/sentinel-ai/releases/latest | grep tag_name
```

Pushing the tag triggers [`.github/workflows/release.yml`](.github/workflows/release.yml),
which runs the gate above and then uses `softprops/action-gh-release` to create the Release
and attach `scripts/install.ps1` and `scripts/install.sh`. Installers resolve
`releases/latest`, so **a tag alone ships nothing** — if the workflow fails, the tag exists,
no Release object appears, and `latest` silently keeps serving the previous version.

### When the workflow fails

Fix the cause and commit it, then either:

- **Re-point the tag** — delete it locally and on the remote, re-tag the green commit, push
  again. Only safe while no Release object exists for that tag; verify with the `releases`
  API first. Nobody can have consumed a tag that never produced a release.
- **Tag a patch** — `vX.Y.Z+1` on the fixed commit. Use this if the release did publish.

Re-running the failed workflow on the same commit does not help: the commit is what failed.

## Things that are easy to get wrong

- **Never** `uv tool install sentinel-ai` from PyPI — that name belongs to an unrelated ML
  toolkit.
- No per-repo config. Remediation messages point at `~/.sentinel-ai/config.toml`.
- `[ai]` is connection details only — base URL, model, credentials. Token and timeout
  budgets live in `[diff_review]`, so tuning one cannot silently move the other.
- Anything added to the `check` path runs on every commit for the whole team, so it must
  fail open and degrade to a warning on internal errors.
- The generated global hook filters by `remote.origin.url` **in shell, before the CLI
  starts**. Starting the CLI costs ~600ms against ~135ms for the shell test, and that tax
  would land on every commit in every unrelated repository on the machine.
- `sentinel.toml` ships in a public repository. Real organisation names belong in
  `~/.sentinel-ai/config.toml`, never in the bundled defaults or in tests.

## Commits

Only commit when asked. Never force-push `main`.

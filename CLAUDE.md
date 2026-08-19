# CLAUDE.md — Sentinel-AI

Context for Claude Code sessions in this repository.

**[`AGENTS.md`](AGENTS.md) is canonical** for roles, review workflow, config layering, and
code rules. Read it before making changes. This file covers what Claude needs most often
and does not repeat what is already there.

## What this tool does

Sentinel-AI runs on the commit boundary and does two orthogonal things:

```
staged index ─┬─ manifest diff → heuristics + trivy → (gated) AI → exit 0|1
              └─ code diff → grounded AI review (network, watermark)
```

Both run inside `sentinel-ai check`, which is the single line every repo's Husky
`pre-commit` hook calls. There is no second hook and no per-repo install step — a feature
reaches a machine when the CLI is updated. Keep it that way when adding layers.

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
- `[diff_review]` carries its own token and timeout budget on purpose; tuning `[ai]` must
  not move it.
- Anything added to the `check` path runs on every commit for the whole team, so it must
  fail open and degrade to a warning on internal errors.

## Commits

Only commit when asked. Never force-push `main`.

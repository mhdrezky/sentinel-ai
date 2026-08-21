# AGENTS.md — Sentinel-AI

Instructions for AI agents working in this repository. **Read this file and `.cursor/rules/` before making changes.**

Claude Code sessions also load `CLAUDE.md`, which points here for anything below and adds
the release procedure and verify commands. This file stays canonical — put shared rules
here, not there.

## Roles

| Agent | When | Do | Don't |
|-------|------|-----|-------|
| **OpenCode** (executor) | Implement, fix, test, **stage** | Follow tasks in `docs/context.md`, run verify, `git add` changed files | Commit unless user asks |
| **Cursor** (reviewer) | Review **staged** diff | `git diff --staged`, update `docs/context.md` | Implement unless user asks |

Default handoff: Cursor reviews → user copies `docs/context.md` → OpenCode executes.

## Project summary

**Sentinel-AI** blocks malicious / typo-squatted / vulnerable dependencies in a git pre-commit hook, and reviews the staged code diff with a local model.

```
staged index ─┬─ manifest diff → heuristics + trivy → exit 0|1
              └─ code diff → grounded AI review (network, watermark)
```

Ecosystems: npm, PyPI, NuGet, Composer.

Both layers run inside `sentinel-ai check` — the one line the `pre-commit` hook calls.
**No second hook, no per-repo install step:** a new capability reaches a machine when the
CLI is updated. Wire new layers into `check` rather than adding hooks.

The dependency side is deterministic (heuristics + Trivy). The AI stage that used to judge
dependency changes was removed; **AI now means the diff reviewer only**.

## Config (critical)

| Layer | Path |
|-------|------|
| Bundled defaults | `src/sentinel_ai/sentinel.toml` (git-tracked, in wheel) |
| Host override | `~/.sentinel-ai/config.toml` |
| Env | `SENTINEL_CONFIG`, `SENTINEL_*` |

**No per-repo config.** Remediation messages must reference `host_config_path()` / `~/.sentinel-ai/config.toml`.

## Install (Windows)

```powershell
irm https://github.com/mhdrezky/sentinel-ai/releases/latest/download/install.ps1 | iex
```

## Install (macOS / Linux)

```bash
curl -fsSL https://github.com/mhdrezky/sentinel-ai/releases/latest/download/install.sh | bash
```

**Never** `uv tool install sentinel-ai` from PyPI — wrong package (ML toolkit, not this repo).

Remote install resolves the **latest release tag** via GitHub API, then:
`git+https://github.com/mhdrezky/sentinel-ai.git@<tag>`

## Releases

A release is a **tag push**. Nothing is created by hand in the GitHub UI; the `v0.1.0`
pattern of uploading a PyInstaller `.exe` is abandoned.

```powershell
# 1. Bump the version in three places
#    pyproject.toml               version = "X.Y.Z"
#    src/sentinel_ai/__init__.py  __version__ = "X.Y.Z"
#    uv.lock                      regenerate via `uv sync --group dev`

# 2. Run the workflow's own gate locally — a failure here is a failed release
uv sync --group dev
uv run pytest -q
uv run ruff check .
uv run ruff format --check .

# 3. Commit, push, tag, push the tag
git push origin main
git tag -a vX.Y.Z -m "vX.Y.Z"
git push origin vX.Y.Z
```

Pushing a `v*` tag triggers `.github/workflows/release.yml`, which reruns that gate and then
uses `softprops/action-gh-release` to create the Release and attach `scripts/install.ps1`
and `scripts/install.sh`.

Confirm it actually published:

```bash
curl -s https://api.github.com/repos/mhdrezky/sentinel-ai/releases/latest | grep tag_name
```

**A tag alone ships nothing.** Installers resolve `releases/latest`, so if the workflow
fails the tag exists, no Release appears, and `latest` keeps serving the previous version
with no error anywhere. There is also **no CI on push to `main`** — only on tags — so
formatting and test rot stay invisible until someone tries to release.

When a release workflow fails: fix the cause, commit it, then either re-point the tag
(delete local + remote, re-tag the green commit — only while no Release object exists for
it, check the `releases` API first) or tag a patch version. Re-running the failed workflow
on the same commit does not help; the commit is what failed.

## Development

```powershell
uv sync --group dev
uv run python -m pytest
uv run ruff check .
uv run ruff format --check .
```

Windows PowerShell 5.x: use `; if ($?) { … }` not `&&`.

## Code rules

1. **Minimal scope** — smallest correct diff.
2. **Match conventions** — read surrounding code first.
3. **No dead code** — remove unused helpers and legacy paths.
4. **Hook latency** — avoid heavy imports on CLI startup.
5. **Fail-open default** — AI/Trivy outages warn, don't block (unless `--strict` / config).

## Cursor rules map

| Rule file | Applies to |
|-----------|------------|
| `.cursor/rules/project-core.mdc` | Always |
| `.cursor/rules/python.mdc` | `src/`, `tests/` |
| `.cursor/rules/install-scripts.mdc` | `scripts/*.ps1` |
| `.cursor/rules/review-handoff.mdc` | Reviews → `docs/context.md` |

## Review workflow

1. OpenCode implements fixes and runs verify, then **`git add`** the changed files.
2. Reviewer reads **`git diff --staged`** (not commits, not unstaged diff):

   ```powershell
   git diff --staged --stat
   git diff --staged
   ```

3. Reviewer updates local **`docs/context.md`** (gitignored; copy from `docs/context.template.md`) with findings + tasks.
4. User copies `docs/context.md` to OpenCode with:

   ```
   Read docs/context.md and AGENTS.md. Execute only the Tasks section. Run Verify commands before done.
   ```

5. OpenCode fixes, runs verify, **stages** again. Repeat until review approves.
6. User commits when satisfied — neither agent commits by default.

## Files not to recreate

- `sentinel.toml.example` — replaced by tracked `src/sentinel_ai/sentinel.toml`
- `scripts/sync-config.ps1` — no longer needed
- `entrypoint.py`, `sentinel.spec` — PyInstaller removed; use `uv tool install`

## Commits

Only commit when user explicitly asks. Never force-push main.

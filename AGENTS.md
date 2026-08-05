# AGENTS.md — Sentinel-AI

Instructions for AI agents working in this repository. **Read this file and `.cursor/rules/` before making changes.**

## Roles

| Agent | When | Do | Don't |
|-------|------|-----|-------|
| **OpenCode** (executor) | Implement, fix, test, **stage** | Follow tasks in `docs/context.md`, run verify, `git add` changed files | Commit unless user asks |
| **Cursor** (reviewer) | Review **staged** diff | `git diff --staged`, update `docs/context.md` | Implement unless user asks |

Default handoff: Cursor reviews → user copies `docs/context.md` → OpenCode executes.

## Project summary

**Sentinel-AI** blocks malicious / typo-squatted / vulnerable dependencies in Husky pre-commit hooks.

```
staged git index → manifest diff → heuristics + trivy → (gated) AI → exit 0|1
```

Ecosystems: npm, PyPI, NuGet, Composer.

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

With project hook:

```powershell
$env:SENTINEL_REPO_PATH = "D:\path\to\project"
irm https://github.com/mhdrezky/sentinel-ai/releases/latest/download/install.ps1 | iex
```

**Never** `uv tool install sentinel-ai` from PyPI — wrong package (ML toolkit, not this repo).

Remote install resolves the **latest release tag** via GitHub API, then:
`git+https://github.com/mhdrezky/sentinel-ai.git@<tag>`

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

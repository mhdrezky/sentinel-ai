# Sentinel-AI

Local supply-chain guard. It sits in a `pre-commit` hook and blocks malicious,
typo-squatted, unpinned, or vulnerable dependencies before they enter a commit —
whether a human or an autonomous coding agent added them.

```
 CRITICAL  Possible typo-squat of express (heuristic)
  package: npm:expres@4.18.0
  `expres` differs from the widely-used `express` by only 1 character on npm.
  Typo-squats rely on exactly this kind of near-miss.
  -> Confirm you meant `expres` and not `express`.
```

Exit code `0` lets the commit through, `1` blocks it.

## How it works

The pipeline is four decoupled stages. Each one only knows about the shared
types in `models.py`.

```
staged index ──▶ manifests ──▶ scanner ──▶ decision engine ──▶ exit code
   gitdiff.py    manifests.py  scanner.py   decision_engine.py
                               heuristics.py
                               + trivy          ▲
                                                │
                                    ai/ ────────┘
                              (on-prem model, gated)
```

1. **`gitdiff.py`** reads the *staged* blob and the `HEAD` blob for each changed
   file. Reading the index rather than the worktree means editing a file after
   `git add` cannot smuggle a package past the check.
2. **`manifests.py`** parses both revisions and diffs them, so only the
   dependencies *this commit introduces* are scanned. Pre-existing findings are
   not the developer's problem right now, and a wall of inherited CVEs trains
   people to reach for `--no-verify`.
3. **`scanner.py`** gathers evidence: offline heuristics plus Trivy for CVEs.
   It never decides anything.
4. **`ai/`** sends the ambiguous cases to the on-prem model server for a
   contextual verdict. It only runs when the deterministic layer found
   something or a new direct dependency appeared — inference is the slowest
   stage and should stay off the hot path.
5. **`decision_engine.py`** applies policy and produces the exit code.

### What the offline layer catches

| Check | Severity | Notes |
|---|---|---|
| Typo-squat of a popular package | `CRITICAL` at edit distance 1, `HIGH` at 2 | Names under 4 chars are skipped as too collision-prone |
| Install-time code execution | `HIGH` | From the lockfile's `hasInstallScript`; only for *newly added* packages |
| Source outside the registry | `HIGH` | `git+`, `http(s)://`, `file:`, `github:` |
| Suspicious project lifecycle script | `CRITICAL` | Network fetch, `eval`, base64, credential paths in `postinstall` etc. |
| No version constraint | `MEDIUM` | `*`, `latest` |
| Denylisted package | `CRITICAL` | From organisation config |

Supported ecosystems: **npm**, **PyPI**, **NuGet**, **Composer**.

`yarn.lock`, `pnpm-lock.yaml`, `poetry.lock` and `Pipfile.lock` are detected but
not yet parsed — Sentinel-AI reports this as degraded coverage rather than
staying silent about it.

## Installing

Pulls the installer from the **latest GitHub release** (same version as the tagged
package). Auto-installs via `uv tool install` (user-level, no admin required),
creates default config at `~/.sentinel-ai/config.toml`.

**Windows**

```powershell
irm https://github.com/mhdrezky/sentinel-ai/releases/latest/download/install.ps1 | iex
```

**macOS / Linux**

```bash
curl -fsSL https://github.com/mhdrezky/sentinel-ai/releases/latest/download/install.sh | bash
```

## Local AI server (on-prem model)

The AI review stage calls an OpenAI-compatible `/chat/completions` endpoint
(vLLM, Ollama, TGI, etc.). Point it at your internal deployment before enabling
the hook in production repos.

**1. Open host config:**

```powershell
notepad $env:USERPROFILE\.sentinel-ai\config.toml
```

**2. Set `base_url` and `model`** under `[ai]` — replace the localhost defaults
with your server:

```toml
[ai]
enabled = true
base_url = "http://10.65.1.119:5003/v1"
model = "Qwen/Qwen3.6-35B-A3B-FP8"
timeout_seconds = 20.0
max_output_tokens = 2048
fail_open = true
enable_thinking = false
```

Use your actual host, port, and model name. The URL must include the `/v1`
suffix when the server exposes an OpenAI-compatible API root.

**3. Verify** the server is reachable:

```powershell
sentinel-ai doctor
sentinel-ai config
```

`doctor` should show `ai:` in green with your `base_url`. The AI stage runs
automatically on pre-commit when heuristic or Trivy findings need contextual
review, or when new direct dependencies are added (unless you pass `--no-ai`).

If the model server is down, commits still proceed by default (`fail_open = true`)
with a warning — deterministic checks still run.

## Trivy (CVE scanning)

Sentinel-AI runs Trivy automatically during `sentinel-ai check` (including the
pre-commit hook) when a commit **introduces new or changed dependencies** in
supported lockfiles. No separate daemon — it is invoked on demand from the
configured binary path.

### Windows setup

**1. Download** Trivy v0.73.0 (64-bit):

```powershell
Invoke-WebRequest -Uri "https://github.com/aquasecurity/trivy/releases/download/v0.73.0/trivy_0.73.0_windows-64bit.zip" -OutFile "$env:USERPROFILE\Downloads\trivy_0.73.0_windows-64bit.zip"
```

**2. Extract** the archive:

```powershell
Expand-Archive -Path "$env:USERPROFILE\Downloads\trivy_0.73.0_windows-64bit.zip" -DestinationPath "$env:USERPROFILE\Downloads\trivy_0.73.0_windows-64bit" -Force
```

**3. Point Sentinel-AI at the binary** in host config:

```powershell
notepad $env:USERPROFILE\.sentinel-ai\config.toml
```

Under `[trivy]`, set `binary_path` to the full path of `trivy.exe` (forward
slashes work in TOML on Windows):

```toml
[trivy]
enabled = true
binary_path = "C:/Users/rezky/Downloads/trivy_0.73.0_windows-64bit/trivy.exe"
```

Adjust the path to match your username and extract folder. Prefer a permanent
location (for example `%LOCALAPPDATA%\trivy\trivy.exe`) over `Downloads` if you
keep Trivy long term.

**4. Verify:**

```powershell
sentinel-ai doctor
```

You should see `trivy:` with a version string. On the next commit that stages a
new or upgraded dependency, Trivy CVE results are included automatically (unless
you pass `--no-trivy`).

If Trivy is missing or misconfigured, Sentinel-AI warns and continues without
CVE checks by default (`trivy.enabled = true` but binary not found).

## Installing into a repository

Sentinel-AI must be on `PATH` (`sentinel-ai doctor`). Then wire Husky in the
project you want to protect:

**1. Install and initialise Husky** (from the repository root):

```bash
npm install --save-dev husky
npx husky init
```

`husky init` creates `.husky/pre-commit` with a sample command. **Do not run**
`sentinel-ai install-hook` here — it refuses to overwrite an existing hook.

**2. Set the pre-commit hook.**

Replace the contents of `.husky/pre-commit` with:

```sh
sentinel-ai check || exit 1
```

Or append that line if you already maintain other checks in the same file.

**3. Verify** from the repository root:

```bash
sentinel-ai doctor
sentinel-ai config
git add .
git commit -m "test: sentinel-ai hook"
```

On Windows, edit with:

```powershell
notepad .husky\pre-commit
```

## Configuration

Defaults ship with the package. Each host overrides via:

```text
~/.sentinel-ai/config.toml     (Windows: %USERPROFILE%\.sentinel-ai\config.toml)
```

Created automatically on first install (`install.ps1` / `install.sh`) from the package's bundled `sentinel.toml`.
Protected repositories only need the Husky hook.

Inspect the active configuration:

```bash
sentinel-ai config
sentinel-ai config --json
```

| Source | Purpose |
|---|---|
| `src/sentinel_ai/sentinel.toml` | **Tracked config** — bundled defaults in the package |
| `~/.sentinel-ai/config.toml` | **Per-host override** — edit AI server, policy |
| `SENTINEL_CONFIG` | Optional explicit config path |
| `SENTINEL_*` env vars | Override for CI/agents |

Precedence (low → high): defaults → bundled → `~/.sentinel-ai/config.toml`
→ `SENTINEL_CONFIG` → env vars.

| Variable | Effect |
|---|---|
| `SENTINEL_CONFIG` | Path to a global config override file |
| `SENTINEL_AI_BASE_URL` | On-prem model server root (OpenAI-compatible) |
| `SENTINEL_AI_MODEL` | Model name on that server |
| `SENTINEL_AI_API_KEY` | Bearer token, if the server requires one |
| `SENTINEL_AI_ENABLED` | `false` disables the AI stage |
| `SENTINEL_TRIVY_PATH` | Path to the Trivy binary |
| `SENTINEL_BLOCK_AT` | Severity floor: `low`/`medium`/`high`/`critical` |

### CLI

```
sentinel-ai check              scan the staged index (the hook's default)
sentinel-ai check --all        scan every manifest on disk
sentinel-ai check --range A..B scan the changes between two refs
sentinel-ai check --json       machine-readable report on stdout
sentinel-ai check --no-ai      skip the AI review stage
sentinel-ai check --strict     block when Sentinel-AI or the model server fails
sentinel-ai doctor             check Trivy and the on-prem model server
sentinel-ai config             show the active organisation configuration
sentinel-ai install-hook       append hook (prefer manual edit if pre-commit exists)
```

## Failure behaviour

The defaults keep a broken dependency out, but do not let Sentinel-AI's own
problems freeze the team:

* **Model server unreachable** → warn, keep the deterministic findings, allow the
  commit (`ai.fail_open = true`).
* **Trivy missing** → warn, skip CVE checks, continue.
* **Internal error** → warn loudly that dependencies were *not* verified,
  allow the commit.

`--strict` (or `ai.fail_open = false`) inverts all three for environments where
an unverified commit is the worse outcome.

Findings carry text from CVE advisories and from the model, so the report is
transliterated to ASCII on consoles that cannot encode it, and untrusted detail
is never parsed as terminal markup.

## Known gaps

* **The Trivy subprocess call has not been run against a real Trivy binary.**
  Trivy was not installed on the machine this was built on. The report
  translation is unit-tested against captured JSON, but the invocation flags
  and the staged-manifest temp tree still need one manual verification. Install
  Trivy and run `sentinel-ai doctor`, then `check --verbose` on a repo with a
  known-vulnerable lockfile.
* `yarn.lock`, `pnpm-lock.yaml`, `poetry.lock` and `Pipfile.lock` are detected
  but not parsed. Repos using them get manifest-level coverage only, and the
  gap is reported at runtime.
* The typo-squat baseline is a small curated list, not registry download
  counts. It catches impersonation of well-known packages; it will not catch a
  squat on an obscure internal dependency.
* No registry metadata is fetched (package age, download count, maintainer
  changes). That would add network latency to every commit and needs its own
  caching design.

## Notes on the AI stage

The AI stage talks to any server that exposes an OpenAI-compatible
`/chat/completions` endpoint — vLLM, Ollama, llama.cpp, TGI, or a hosted
OpenAI-compatible gateway. Point `base_url` and `model` at your deployment;
no vendor-specific integration is required.

The system prompt tells the model that everything inside `<evidence>` is
untrusted data, not instruction. A package can put text in its own description
or install script addressed at an LLM reviewer; the prompt treats such text as a
malicious indicator in its own right.

A verdict below 0.5 confidence is demoted one severity step. The model advises;
it should not be the sole reason a developer's commit fails.

## Development

Bundled defaults come from [`src/sentinel_ai/sentinel.toml`](src/sentinel_ai/sentinel.toml)
(tracked in git).
Per-machine overrides live in `~/.sentinel-ai/config.toml` (auto-created by
`install.ps1` / `install.sh`).

```bash
uv sync --group dev
```

```bash
uv run pytest
```

On Windows, if `uv run pytest` fails with a trampoline error, recreate the
virtual environment (`Remove-Item -Recurse -Force .venv` then `uv sync
--group dev`) or run `uv run python -m pytest` instead.

```bash
# bash / PowerShell 7+
uv run ruff check . && uv run ruff format --check .
```

```powershell
# Windows PowerShell 5.x (`&&` is not supported)
uv run ruff check .; if ($?) { uv run ruff format --check . }
```

Run it against a repository without installing anything:

```bash
uv run sentinel-ai check --repo /path/to/project --verbose
```

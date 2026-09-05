# Sentinel-AI

[![Release](https://img.shields.io/github/v/release/mhdrezky/sentinel-ai)](https://github.com/mhdrezky/sentinel-ai/releases/latest)
[![CI](https://img.shields.io/github/actions/workflow/status/mhdrezky/sentinel-ai/release.yml?branch=main&label=CI)](https://github.com/mhdrezky/sentinel-ai/actions/workflows/release.yml)

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

Two paths run from the staged index. The dependency path is four decoupled
stages; each one only knows about the shared types in `models.py`. The code
path is a grounded AI review of that same index.

```
staged index ─┬─ manifest diff ──▶ scanner ──▶ decision engine ──▶ exit 0|1
              │  gitdiff.py        scanner.py      decision_engine.py
              │  manifests.py      heuristics.py
              │                    + trivy
              │
              └─ code diff ──────▶ grounded AI review (network, watermark)
                                   diff_review/
```

The dependency path:

1. **`gitdiff.py`** reads the *staged* blob and the `HEAD` blob for each changed
   file. Reading the index rather than the worktree means editing a file after
   `git add` cannot smuggle a package past the check.
2. **`manifests.py`** parses both revisions and diffs them, so only the
   dependencies *this commit introduces* are scanned. Pre-existing findings are
   not the developer's problem right now, and a wall of inherited CVEs trains
   people to reach for `--no-verify`.
3. **`scanner.py`** gathers evidence: offline heuristics plus Trivy for CVEs.
   It never decides anything.
4. **`decision_engine.py`** applies policy and produces the exit code.

The code path (`diff_review/`) is independent of that chain. It reviews the
staged **code** diff with the on-prem model — new outbound URLs, and AI
attribution left in the source. Manifests and lockfiles are stripped out of
what the model sees; dependencies are not sent to it. Inference stays off the
hot path when the remaining diff is empty. `check --all` and `check --range`
skip this path: they answer a different question, and this layer only reads
the index.

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

Lockfiles read in full: `package-lock.json`, `npm-shrinkwrap.json`, `composer.lock`,
and `uv.lock`. A lockfile matters more than it looks — `pyproject.toml` holds version
ranges, and CVE matching needs the exact resolved version, so without one Trivy has
nothing to work with on a Python project.

`yarn.lock`, `pnpm-lock.yaml`, `poetry.lock` and `Pipfile.lock` are detected but not
yet parsed — Sentinel-AI reports this as degraded coverage rather than staying silent
about it.

## Installing

Pulls the installer from the **latest GitHub release** (same version as the tagged
package). Auto-installs via `uv tool install` (user-level, no admin required),
creates default config at `~/.sentinel-ai/config.toml`, and downloads the latest
Trivy binary into `~/.sentinel-ai/bin/` when possible.

**Windows**

```powershell
irm https://github.com/mhdrezky/sentinel-ai/releases/latest/download/install.ps1 | iex
```

**macOS / Linux**

```bash
curl -fsSL https://github.com/mhdrezky/sentinel-ai/releases/latest/download/install.sh | bash
```

Upgrade an existing install:

```bash
sentinel-ai update
```

**On Windows, use the installer above instead.** The CLI runs from inside the
environment uv would replace, and Windows locks the running interpreter — but uv
deletes the packages before it reaches that lock, so a failed self-update leaves no
working CLI at all rather than the previous version. From 0.3.1 the command refuses
and prints the way out; before that it simply breaks, and the repair is:

```powershell
uv tool install --force "git+https://github.com/mhdrezky/sentinel-ai.git@v0.3.1"
```

Remove Sentinel-AI from this machine — host config, Trivy binary, the machine-wide
hook, and the uv tool:

```bash
sentinel-ai uninstall --yes
```

It unsets `core.hooksPath` when `install-global-hook` was the one that set it, and
leaves it alone when something else owns it. Hooks added to project repositories —
Husky and the like — are never touched.

On Windows the last step cannot finish from inside the CLI: the running interpreter
lives in the environment being removed, and Windows locks it. Everything else is
removed and the command tells you to finish with `uv tool uninstall sentinel-ai`.

### Removing it by hand

Versions before 0.3.1 have no `uninstall` command. This is the same work:

**Windows (PowerShell):**

```powershell
git config --global --unset core.hooksPath
uv tool uninstall sentinel-ai
Remove-Item -Recurse -Force "$env:USERPROFILE\.sentinel-ai"
```

**macOS / Linux:**

```bash
git config --global --unset core.hooksPath
uv tool uninstall sentinel-ai
rm -rf ~/.sentinel-ai
```

Skip the first line unless you ran `install-global-hook` — unlike the command, it
does not check who set `core.hooksPath` before clearing it, so it would also
disconnect another tool that relies on it.

## Local AI server (on-prem model)

The AI reviews the staged **code diff** — new outbound URLs, and AI attribution left
in the source. Dependencies are checked deterministically by heuristics and Trivy, with
no model involved.

It calls an OpenAI-compatible `/chat/completions` endpoint (vLLM, Ollama, TGI, etc.).
Point it at your internal deployment before enabling the hook in production repos.

**1. Open host config:**

```bash
sentinel-ai config edit
```

Opens `~/.sentinel-ai/config.toml` in Notepad (Windows), TextEdit (macOS), or
`$EDITOR` / `xdg-open` (Linux).

**2. Set `base_url` and `model`** under `[ai]` — replace the localhost defaults
with your server:

```toml
[ai]
enabled = true
base_url = "http://your-model-host:5003/v1"
model = "Qwen/Qwen3.6-35B-A3B-FP8"
enable_thinking = false
```

`[ai]` holds connection details only. The review's own limits live in `[diff_review]`,
so changing one cannot silently move the other:

```toml
[diff_review]
enabled = true
fail_open = true
max_output_tokens = 256
timeout_seconds = 12.0
max_diff_bytes = 40_000
```

Use your actual host, port, and model name. The URL must include the `/v1`
suffix when the server exposes an OpenAI-compatible API root.

**3. Verify** the server is reachable:

```powershell
sentinel-ai doctor
sentinel-ai config
```

`doctor` should show `ai:` in green with your `base_url`. The AI stage reviews
the staged code diff automatically on pre-commit when `[diff_review]` and `[ai]`
are enabled (unless you pass `--no-ai`). A dependency-only commit with an empty
code diff does not call the model.

If the model server is down, commits still proceed by default (`fail_open = true`)
with a warning — deterministic checks still run.

## Trivy (CVE scanning)

Sentinel-AI runs Trivy automatically during `sentinel-ai check` (including the
pre-commit hook) when a commit **introduces new or changed dependencies** in
supported lockfiles. No separate daemon — it is invoked on demand from the
configured binary path.

The Windows/macOS/Linux installers download the **latest Trivy release** into
`~/.sentinel-ai/bin/` and set `[trivy].binary_path` in host config automatically.
If that step fails, install manually:

### Manual Trivy setup (fallback)

**Windows** — download the latest `trivy_*_windows-64bit.zip` from
[GitHub releases](https://github.com/aquasecurity/trivy/releases), extract
`trivy.exe` to `%USERPROFILE%\.sentinel-ai\bin\`, then set in host config:

```toml
[trivy]
enabled = true
binary_path = "C:/Users/you/.sentinel-ai/bin/trivy.exe"
```

**macOS / Linux** — download the matching `trivy_*` archive for your platform,
place the `trivy` binary in `~/.sentinel-ai/bin/`, `chmod +x` it, then:

```toml
[trivy]
enabled = true
binary_path = "/home/you/.sentinel-ai/bin/trivy"
```

Verify:

```bash
sentinel-ai doctor
```

You should see `trivy:` with a version string. On the next commit that stages a
new or upgraded dependency, Trivy CVE results are included automatically (unless
you pass `--no-trivy`).

If Trivy is missing or misconfigured, Sentinel-AI warns and continues without
CVE checks by default (`trivy.enabled = true` but binary not found).

## Installing the hook

Sentinel-AI must be on `PATH` first — check with `sentinel-ai doctor`.

Git never installs hooks when you clone: a repository must not be able to run code on
you just for cloning it. So a hook has to be set up once per working copy. Across a
dozen repositories and several machines that rarely happens, which is why the
recommended route sets it up **once per machine** instead.

### Recommended: one hook for every repository on the machine

```bash
sentinel-ai install-global-hook --org your-org
```

This writes `~/.sentinel-ai/hooks/pre-commit` and points git's global `core.hooksPath`
at it. Every repository on the machine is then covered — including ones cloned later —
with no per-repository step and no npm.

`--org` is matched against `remote.origin.url`, so the hook stays out of personal
projects. It is repeatable, and the check runs in shell before the CLI starts, so an
unrelated commit costs milliseconds rather than a second. Repositories with no `origin`
are skipped.

Set the default once instead of passing the flag every time:

```toml
# ~/.sentinel-ai/config.toml
[hook]
organizations = ["your-org"]
```

Then `sentinel-ai install-global-hook` needs no arguments. There is no default in the
shipped config — the file lives in a public repository, so your organisation name
belongs in the host config.

To cover **every** repository on the machine, personal ones included:

```bash
sentinel-ai install-global-hook --all
```

To reverse it:

```bash
sentinel-ai uninstall-global-hook
```

Two things worth knowing. `core.hooksPath` replaces the whole hooks directory, so a
hand-written `.git/hooks/pre-push` stops running. And after editing
`hook.organizations` you must re-run the install command — the list is baked into the
generated script; `sentinel-ai doctor` warns when the two have drifted apart.

### Alternative: per-repository with Husky

For a repository that already uses Husky, or where you want the hook visible in the
project itself:

```bash
npm install --save-dev husky
npx husky init
sentinel-ai install-hook
```

`install-hook` appends `sentinel-ai check || exit 1` to `.husky/pre-commit`, or reports
that it is already present. You can also add that line by hand.

A repository-local `core.hooksPath` — which is what Husky sets — wins over the global
one, so the two do not fight.

### Verify

```bash
sentinel-ai doctor
git add .
git commit -m "test: sentinel-ai hook"
```

## Configuration

Defaults ship with the package. Each host overrides via:

```text
~/.sentinel-ai/config.toml     (Windows: %USERPROFILE%\.sentinel-ai\config.toml)
```

Created automatically on first install (`install.ps1` / `install.sh`) from the package's bundled `sentinel.toml`.
Protected repositories need no configuration of their own.

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
sentinel-ai check --no-ai      skip the AI diff review
sentinel-ai check --strict     block when Sentinel-AI or the model server fails
sentinel-ai diff-review        review the staged code diff on its own
sentinel-ai doctor             check Trivy, the model server, and the hook
sentinel-ai config             show the active organisation configuration
sentinel-ai config edit        open host config in the default editor
sentinel-ai update             upgrade the CLI (not on Windows — see above)
sentinel-ai uninstall --yes    remove CLI and ~/.sentinel-ai from this host
sentinel-ai install-hook             append sentinel-ai to a Husky pre-commit
sentinel-ai install-global-hook      cover every repo on this machine
sentinel-ai uninstall-global-hook    undo the machine-wide hook
```

## Failure behaviour

The defaults keep a broken dependency out, but do not let Sentinel-AI's own
problems freeze the team:

* **Model server unreachable** → warn, keep the deterministic findings, allow the
  commit (`diff_review.fail_open = true`). `--strict` or `fail_open = false`
  blocks instead.
* **Trivy missing or failing** → warn, skip CVE checks, continue. Neither
  `--strict` nor `fail_open` changes this.
* **Internal error in the dependency scanner** → warn loudly that dependencies
  were *not* verified, allow the commit. `--strict` blocks; `fail_open` does
  not apply here.

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
  but not parsed. Repos using them get manifest-level coverage only — direct
  dependencies but no transitive ones — and the gap is reported at runtime.
* Legacy .NET projects keep their dependencies in `packages.config`; a non-SDK
  `.csproj` carries assembly references rather than `PackageReference`, so a
  project without a `packages.config` contributes nothing.
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

The system prompt tells the model that everything inside the nonce-tagged
`<diff>` region is untrusted data, not instruction. A commit can put text in
the diff addressed at an LLM reviewer; the prompt treats that text as content
to inspect, not as instruction to follow.

The model's own `v` field is ignored. A finding whose snippet cannot be found
in an added line is dropped. Only a grounded `critical` finding blocks; the
rest are recorded as notices. The model proposes; the diff decides.

## Engineering decisions

Each entry is the constraint, the choice, and the trade-off accepted.

### Installers are validated in CI, not executed

The installers ship verbatim as release assets. A syntax error in `install.sh`
or `install.ps1` reaches users as a broken one-liner they paste into a shell.
The release workflow therefore parses them on every run (push to `main`, a
`v*` tag, or a manual dispatch) before a tag can publish: `bash -n` for the
shell script, and the PowerShell AST parser for `install.ps1`. Installer
upload happens only on a tag push. Nothing else executes them before a user
does — a parse-only gate, not a dry-run against a real machine.

### Only new dependencies are scanned

The hook diffs `HEAD` against the staged index and scans what this commit
introduces. Scanning the whole tree would dump a wall of inherited CVEs onto
every commit and train people to reach for `--no-verify`. The trade-off is
accepted in the open: pre-existing vulnerabilities are not the hook's job.

### The staged index is read, not the worktree

Git commits the index, not the files on disk. Reading staged blobs against
`HEAD` means editing a file after `git add` cannot smuggle a package past the
check. Unstaged edits are invisible here — the same contract git itself has.

### Fail-open by default

A model server outage, a missing Trivy binary, or an internal error warns and
lets the commit through rather than freezing the team. Deterministic checks
still run. `--strict` inverts the model-server and scanner-bug cases, where an
unverified commit is the worse outcome. `fail_open = false` only inverts an
unreachable model server. A missing Trivy binary never blocks.

### Prompt injection is content, not instruction

Everything sent to the model is untrusted data. The staged diff is wrapped in a
nonce-tagged `<diff>` delimiter so a forged closing tag cannot end the untrusted
region and address the model as itself. The system prompt tells the reviewer
that text addressed at it is content to inspect, not instruction to follow.

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

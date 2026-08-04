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
| Denylisted package | `CRITICAL` | From `.sentinel.toml` |

Supported ecosystems: **npm**, **PyPI**, **NuGet**, **Composer**.

`yarn.lock`, `pnpm-lock.yaml`, `poetry.lock` and `Pipfile.lock` are detected but
not yet parsed — Sentinel-AI reports this as degraded coverage rather than
staying silent about it.

## Development

```bash
uv sync
```

```bash
uv run pytest
```

```bash
uv run ruff check . && uv run ruff format --check .
```

Run it against a repository without installing anything:

```bash
uv run sentinel-ai check --repo /path/to/project --verbose
```

## Building the binary

```bash
uv run pyinstaller sentinel.spec --clean --noconfirm
```

Produces a single `dist/sentinel-ai` (or `.exe`, ~16 MB) with no runtime
dependency on Python.

One-file mode costs roughly a second of startup on each run, because the
bootloader unpacks to a temp directory. If that becomes the dominant cost in
the hook, switch the spec to one-dir mode (`COLLECT`) — startup drops to about
0.3 s at the price of distributing a folder instead of a file.

## Installing into a repository

Sentinel-AI needs to be on `PATH`, then:

```bash
npx husky init
```

```bash
sentinel-ai install-hook
```

That writes `.husky/pre-commit`:

```sh
#!/usr/bin/env sh
sentinel-ai check || exit 1
```

Verify the local environment at any time:

```bash
sentinel-ai doctor
```

## Configuration

Copy `.sentinel.toml.example` to `.sentinel.toml` in the protected repository
and commit it — the policy deserves review like any other code. Every value has
a working default, so the file is optional.

Environment variables override the file, which is what CI and agent runners
should use:

| Variable | Effect |
|---|---|
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
sentinel-ai install-hook       write the Husky pre-commit hook
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

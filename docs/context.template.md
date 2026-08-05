# Review Context — Sentinel-AI

> Copy this file (or `docs/context.md`) to OpenCode executor agent.
> Prefix prompt: `Read docs/context.md and AGENTS.md. Execute only the Tasks section. Run Verify commands before done.`

---

## Meta

| Field | Value |
|-------|-------|
| **Date** | YYYY-MM-DD |
| **Reviewer** | Cursor / human |
| **Branch** | `main` |
| **Review basis** | `git diff --staged` |
| **Staged files** | from `git diff --staged --stat` |
| **Status** | `approve` \| `changes-requested` \| `blocked` |
| **Scope** | e.g. install.ps1, README, release workflow |

---

## Staged diff (reference)

```powershell
git diff --staged --stat
```

<!-- Paste stat output or list files here when handing off -->

---

## Summary

One paragraph: what was reviewed and overall verdict.

---

## Findings

### Blockers

<!-- Must fix before merge / release -->

1. **[blocker]** `path/to/file` — description. **Fix:** expected change.

### Major

1. **[major]** …

### Minor

1. **[minor]** …

---

## Tasks for OpenCode

<!-- Copy-paste checklist — executor marks [x] when done -->

- [ ] Task 1: …
- [ ] Task 2: …

---

## Verify

Run after all tasks:

```powershell
uv run python -m pytest -q
uv run ruff check .
uv run ruff format --check .
```

Manual (if install changed):

```powershell
# Dry-run on clean machine or VM
irm https://github.com/mhdrezky/sentinel-ai/releases/latest/download/install.ps1 | iex
sentinel-ai doctor
sentinel-ai config
```

---

## Out of scope

What reviewer explicitly did NOT ask to change.

---

## Notes for next review

Open questions or follow-ups.

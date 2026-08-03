"""Deterministic, offline checks that run before (and cheaply gate) the LLM.

These are the fast path: no network, no subprocess. They catch the attack
patterns that have clear structural signatures — typo-squatting, install-time
code execution, and dependencies sourced from outside the registry.

Anything genuinely ambiguous is left for the AI layer to judge.
"""

from __future__ import annotations

import re

from .config import PolicyConfig
from .data.popular_packages import popular_for
from .manifests import ParsedManifest
from .models import (
    ChangeType,
    Ecosystem,
    Finding,
    FindingSource,
    PackageChange,
    Severity,
)

# Version specifiers that pull code from somewhere the registry never vetted.
_NONREGISTRY_PREFIXES = (
    "git://",
    "git+",
    "http://",
    "https://",
    "file:",
    "link:",
    "github:",
    "gitlab:",
    "bitbucket:",
    "portal:",
)

# npm lifecycle hooks that execute on `npm install`, before any code review.
INSTALL_HOOKS = ("preinstall", "install", "postinstall", "prepare", "prepublish")

# Commands inside an install script that are worth a human's attention.
_SUSPICIOUS_SCRIPT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bcurl\b|\bwget\b", re.I), "downloads a remote payload"),
    (re.compile(r"\b(?:base64|atob|b64decode)\b", re.I), "decodes obfuscated data"),
    (re.compile(r"\beval\b|new\s+Function\s*\(", re.I), "evaluates dynamic code"),
    (re.compile(r"child_process|\bexec(?:Sync)?\s*\(", re.I), "spawns a subprocess"),
    (
        re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),
        "contacts a hard-coded IP address",
    ),
    (
        re.compile(r"process\.env|~/\.ssh|\.aws/credentials|\.npmrc", re.I),
        "reads credentials or environment secrets",
    ),
    (re.compile(r"\bnc\b\s|\bnetcat\b|/dev/tcp/", re.I), "opens a network shell"),
    (re.compile(r"chmod\s+\+x|\bsudo\b", re.I), "escalates file or user privileges"),
]

_UNPINNED_SPECS = {"*", "latest", "", "x", "*.*.*"}


def evaluate(
    changes: list[PackageChange],
    manifests: dict[str, ParsedManifest],
    policy: PolicyConfig,
) -> list[Finding]:
    """Run every heuristic over the changed packages.

    `manifests` maps path -> parsed staged manifest, so lockfile metadata
    (install-script flags) can inform findings about direct dependencies.
    """
    findings: list[Finding] = []
    install_script_names = _collect_install_script_packages(manifests)

    for change in changes:
        if policy.is_allowlisted(change.name, change.coordinate):
            continue

        if finding := _check_denylist(change, policy):
            findings.append(finding)
            # A denylisted package needs no further explanation.
            continue

        findings.extend(
            f
            for f in (
                _check_typosquat(change),
                _check_nonregistry_source(change),
                _check_install_script(change, install_script_names),
                _check_unpinned(change),
            )
            if f is not None
        )

    findings.extend(_check_root_scripts(manifests))
    return findings


def _check_denylist(change: PackageChange, policy: PolicyConfig) -> Finding | None:
    bare = change.name.lower()
    if not any(
        entry.lower() in (bare, change.coordinate.lower()) for entry in policy.denylist
    ):
        return None
    return Finding(
        source=FindingSource.POLICY,
        severity=Severity.CRITICAL,
        title=f"{change.name} is on the organisation denylist",
        detail=(
            f"`{change.name}` is explicitly banned by this repository's "
            f"`.sentinel.toml` policy."
        ),
        package=change.coordinate,
        remediation="Remove the dependency, or raise an exception with the security team.",
    )


def _check_typosquat(change: PackageChange) -> Finding | None:
    """Flag names within edit distance 2 of a well-known package."""
    name = change.name.lower()
    popular = popular_for(change.ecosystem)
    if name in popular:
        return None
    # Short names collide by chance far too often to judge this way.
    if len(name) < 4:
        return None

    nearest, distance = _nearest_popular(name, popular)
    if nearest is None:
        return None

    severity = Severity.CRITICAL if distance == 1 else Severity.HIGH
    return Finding(
        source=FindingSource.HEURISTIC,
        severity=severity,
        title=f"Possible typo-squat of {nearest}",
        detail=(
            f"`{change.name}` differs from the widely-used "
            f"`{nearest}` by only {distance} character"
            f"{'s' if distance > 1 else ''} on {change.ecosystem.registry_name}. "
            f"Typo-squats rely on exactly this kind of near-miss."
        ),
        package=change.coordinate,
        remediation=f"Confirm you meant `{change.name}` and not `{nearest}`.",
    )


def _nearest_popular(name: str, popular: frozenset[str]) -> tuple[str | None, int]:
    """Closest popular package within distance 2, preferring the closest match."""
    best: str | None = None
    best_distance = 3
    for candidate in popular:
        # Length gap alone can rule a candidate out without any real work.
        if abs(len(candidate) - len(name)) > 2:
            continue
        distance = _bounded_levenshtein(name, candidate, max_distance=2)
        if distance < best_distance:
            best, best_distance = candidate, distance
            if best_distance == 1:
                break
    return (best, best_distance) if best is not None else (None, 3)


def _bounded_levenshtein(left: str, right: str, max_distance: int) -> int:
    """Edit distance, abandoning early once it provably exceeds `max_distance`.

    Returns `max_distance + 1` to signal "further than we care about".
    """
    if left == right:
        return 0
    if abs(len(left) - len(right)) > max_distance:
        return max_distance + 1

    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, start=1):
        current = [i]
        for j, right_char in enumerate(right, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (left_char != right_char),
                )
            )
        if min(current) > max_distance:
            return max_distance + 1
        previous = current
    return previous[-1]


def _check_nonregistry_source(change: PackageChange) -> Finding | None:
    spec = (change.new_version or "").strip().lower()
    if not spec.startswith(_NONREGISTRY_PREFIXES):
        return None
    return Finding(
        source=FindingSource.HEURISTIC,
        severity=Severity.HIGH,
        title=f"{change.name} is installed from outside the registry",
        detail=(
            f"`{change.name}` resolves to `{change.new_version}`, bypassing "
            f"{change.ecosystem.registry_name} entirely. That source can change "
            f"its contents at any time without a version bump, and it is not "
            f"covered by registry malware scanning."
        ),
        package=change.coordinate,
        remediation=(
            "Pin to a published registry release, or vendor the code into "
            "this repository where it can be reviewed."
        ),
    )


def _check_install_script(
    change: PackageChange, install_script_names: set[str]
) -> Finding | None:
    if change.name not in install_script_names:
        return None
    # Only *new* packages are flagged; an existing dep's install hook has
    # already run on every machine and is not news.
    if change.change_type is not ChangeType.ADDED:
        return None
    return Finding(
        source=FindingSource.HEURISTIC,
        severity=Severity.HIGH,
        title=f"{change.name} executes code at install time",
        detail=(
            f"The lockfile marks `{change.name}` as having an install hook. "
            f"It will run arbitrary code on every developer and CI machine as "
            f"soon as this commit lands, before anyone reviews it."
        ),
        package=change.coordinate,
        remediation=(
            "Read the package's install script. If it is legitimate, add "
            f"`{change.name}` to `allowlist` in .sentinel.toml."
        ),
    )


def _check_unpinned(change: PackageChange) -> Finding | None:
    spec = (change.new_version or "").strip().lower()
    if spec not in _UNPINNED_SPECS:
        return None
    return Finding(
        source=FindingSource.HEURISTIC,
        severity=Severity.MEDIUM,
        title=f"{change.name} has no version constraint",
        detail=(
            f"`{change.name}` is declared as `{change.new_version or '(empty)'}`, "
            f"so any future release — including a compromised one — installs "
            f"automatically."
        ),
        package=change.coordinate,
        remediation="Pin to an explicit version or a bounded range.",
    )


def _check_root_scripts(manifests: dict[str, ParsedManifest]) -> list[Finding]:
    """Inspect lifecycle hooks declared by the repository's own package.json.

    An agent editing `postinstall` in the project manifest is a direct code
    execution path, and no registry scanner would ever see it.
    """
    findings: list[Finding] = []
    for path, manifest in manifests.items():
        if manifest.ecosystem is not Ecosystem.NPM or not manifest.scripts:
            continue
        for hook in INSTALL_HOOKS:
            body = manifest.scripts.get(hook)
            if not body:
                continue
            reasons = [
                reason
                for pattern, reason in _SUSPICIOUS_SCRIPT_PATTERNS
                if pattern.search(body)
            ]
            if not reasons:
                continue
            findings.append(
                Finding(
                    source=FindingSource.HEURISTIC,
                    severity=Severity.CRITICAL,
                    title=f"Suspicious `{hook}` script in {path}",
                    detail=(
                        f"The `{hook}` hook {_join(reasons)}.\n    {body.strip()[:400]}"
                    ),
                    package=path,
                    remediation=(
                        "Remove the hook, or move the logic into an explicit "
                        "script a developer runs deliberately."
                    ),
                )
            )
    return findings


def describe_script_risk(script_body: str) -> list[str]:
    """Public helper — the AI layer reuses this to pre-tag scripts it sends."""
    return [
        reason
        for pattern, reason in _SUSPICIOUS_SCRIPT_PATTERNS
        if pattern.search(script_body)
    ]


def _collect_install_script_packages(
    manifests: dict[str, ParsedManifest],
) -> set[str]:
    names: set[str] = set()
    for manifest in manifests.values():
        names |= manifest.install_script_packages
    return names


def _join(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f", and {items[-1]}"

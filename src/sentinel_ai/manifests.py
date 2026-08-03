"""Detect dependency manifests in a diff and work out what actually changed.

The goal is to narrow a commit down to *only the dependencies a developer or
agent just introduced*. Scanning the whole dependency tree on every commit
would be far too slow for a pre-commit hook, and would drown real signal in
pre-existing noise.

Supported today: npm, PyPI, NuGet, Composer. `yarn.lock` and `pnpm-lock.yaml`
are recognised but not parsed — see `UNPARSED_LOCKFILES`.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from xml.etree import ElementTree

from .models import ChangeType, Ecosystem, PackageChange

# Lockfiles we can spot but not yet read. Surfaced as a degraded-mode warning
# rather than silently ignored, so nobody assumes they were checked.
UNPARSED_LOCKFILES: dict[str, Ecosystem] = {
    "yarn.lock": Ecosystem.NPM,
    "pnpm-lock.yaml": Ecosystem.NPM,
    "poetry.lock": Ecosystem.PYPI,
    "Pipfile.lock": Ecosystem.PYPI,
    "packages.lock.json": Ecosystem.NUGET,
}


@dataclass
class ParsedManifest:
    """Normalised view of one manifest file."""

    ecosystem: Ecosystem
    dependencies: dict[str, str] = field(default_factory=dict)
    """name -> raw version specifier, exactly as written."""
    is_lockfile: bool = False
    scripts: dict[str, str] = field(default_factory=dict)
    """npm `scripts` block, from package.json only."""
    install_script_packages: set[str] = field(default_factory=set)
    """Packages a lockfile flags as running install hooks (`hasInstallScript`)."""


def identify(path: str) -> Ecosystem | None:
    """Ecosystem for a repo-relative path, or None if it is not a manifest."""
    name = PurePosixPath(path).name
    if name in _EXACT_MATCHES:
        return _EXACT_MATCHES[name]
    if name in UNPARSED_LOCKFILES:
        return UNPARSED_LOCKFILES[name]
    if _REQUIREMENTS_RE.match(name):
        return Ecosystem.PYPI
    if name.endswith((".csproj", ".fsproj", ".vbproj")):
        return Ecosystem.NUGET
    return None


def is_parseable(path: str) -> bool:
    return PurePosixPath(path).name not in UNPARSED_LOCKFILES


def find_manifests(paths: list[str]) -> list[str]:
    """Filter a list of changed files down to dependency manifests.

    Vendored trees are skipped — a `node_modules/` checkin is its own problem,
    but scanning it here would take minutes.
    """
    return [
        path for path in paths if identify(path) is not None and not _is_vendored(path)
    ]


def parse(path: str, content: str) -> ParsedManifest | None:
    """Parse manifest `content`. Returns None for unsupported or broken files.

    Parse failures are deliberately soft: a half-written package.json mid-merge
    should not crash the hook. The caller reports it as degraded coverage.
    """
    ecosystem = identify(path)
    if ecosystem is None or not is_parseable(path):
        return None

    name = PurePosixPath(path).name
    try:
        if name == "package.json":
            return _parse_package_json(content)
        if name in ("package-lock.json", "npm-shrinkwrap.json"):
            return _parse_package_lock(content)
        if name == "composer.json":
            return _parse_composer_json(content)
        if name == "composer.lock":
            return _parse_composer_lock(content)
        if name == "pyproject.toml":
            return _parse_pyproject(content)
        if _REQUIREMENTS_RE.match(name):
            return _parse_requirements(content)
        if name == "packages.config":
            return _parse_packages_config(content)
        if name.endswith((".csproj", ".fsproj", ".vbproj")):
            return _parse_csproj(content)
    except (json.JSONDecodeError, tomllib.TOMLDecodeError, ElementTree.ParseError):
        return None
    return None


def diff_manifests(
    path: str,
    old_content: str | None,
    new_content: str | None,
) -> list[PackageChange]:
    """Dependencies added or version-changed between two revisions of a manifest.

    Removals are dropped. When `old_content` is None the manifest is new, so
    everything in it counts as added.
    """
    if new_content is None:
        return []
    new = parse(path, new_content)
    if new is None:
        return []
    old = parse(path, old_content) if old_content else None
    previous = old.dependencies if old else {}

    changes: list[PackageChange] = []
    for name, version in new.dependencies.items():
        if name not in previous:
            change_type = ChangeType.ADDED
            old_version = None
        elif previous[name] != version:
            change_type = _classify_bump(previous[name], version)
            old_version = previous[name]
        else:
            continue

        changes.append(
            PackageChange(
                name=name,
                ecosystem=new.ecosystem,
                new_version=version,
                old_version=old_version,
                change_type=change_type,
                manifest_path=path,
                is_direct=not new.is_lockfile,
            )
        )
    return changes


def _classify_bump(old_version: str, new_version: str) -> ChangeType:
    """Upgrade vs downgrade, comparing numeric release segments only.

    Anything non-numeric (git refs, ranges, prereleases) falls back to UPGRADED,
    which is the conservative choice: it keeps the package in the scan set.
    """
    old_parts = _numeric_parts(old_version)
    new_parts = _numeric_parts(new_version)
    if old_parts and new_parts and new_parts < old_parts:
        return ChangeType.DOWNGRADED
    return ChangeType.UPGRADED


def _numeric_parts(version: str) -> tuple[int, ...]:
    match = _VERSION_CORE_RE.search(version)
    if not match:
        return ()
    return tuple(int(part) for part in match.group(0).split("."))


# --------------------------------------------------------------------------- npm


def _parse_package_json(content: str) -> ParsedManifest:
    data = json.loads(content)
    deps: dict[str, str] = {}
    # peerDependencies are intentionally excluded: they declare a contract with
    # the host app rather than pulling anything into the tree.
    for block in ("dependencies", "devDependencies", "optionalDependencies"):
        section = data.get(block)
        if isinstance(section, dict):
            deps.update({str(k): str(v) for k, v in section.items()})

    scripts = data.get("scripts")
    return ParsedManifest(
        ecosystem=Ecosystem.NPM,
        dependencies=deps,
        scripts={str(k): str(v) for k, v in scripts.items()}
        if isinstance(scripts, dict)
        else {},
    )


def _parse_package_lock(content: str) -> ParsedManifest:
    data = json.loads(content)
    deps: dict[str, str] = {}
    install_scripts: set[str] = set()

    packages = data.get("packages")
    if isinstance(packages, dict):
        # lockfile v2/v3: keys are install paths; "" is the root project itself.
        for install_path, meta in packages.items():
            if not install_path or not isinstance(meta, dict):
                continue
            name = meta.get("name") or _name_from_node_modules_path(install_path)
            version = meta.get("version")
            if not name or not version:
                continue
            deps[str(name)] = str(version)
            if meta.get("hasInstallScript"):
                install_scripts.add(str(name))
    else:
        # lockfile v1: a nested "dependencies" tree.
        _flatten_lock_v1(data.get("dependencies"), deps)

    return ParsedManifest(
        ecosystem=Ecosystem.NPM,
        dependencies=deps,
        is_lockfile=True,
        install_script_packages=install_scripts,
    )


def _flatten_lock_v1(node: object, out: dict[str, str]) -> None:
    if not isinstance(node, dict):
        return
    for name, meta in node.items():
        if not isinstance(meta, dict):
            continue
        version = meta.get("version")
        if version:
            out[str(name)] = str(version)
        _flatten_lock_v1(meta.get("dependencies"), out)


def _name_from_node_modules_path(install_path: str) -> str | None:
    """`node_modules/@scope/pkg/node_modules/dep` -> `dep`."""
    marker = "node_modules/"
    index = install_path.rfind(marker)
    if index == -1:
        return None
    return install_path[index + len(marker) :] or None


# -------------------------------------------------------------------------- PyPI


def _parse_requirements(content: str) -> ParsedManifest:
    deps: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            # Skips -r includes, -e editables, and pip flags.
            continue
        match = _REQUIREMENT_RE.match(line)
        if match:
            deps[match.group("name")] = (match.group("spec") or "").strip() or "*"
    return ParsedManifest(ecosystem=Ecosystem.PYPI, dependencies=deps)


def _parse_pyproject(content: str) -> ParsedManifest:
    data = tomllib.loads(content)
    deps: dict[str, str] = {}

    # PEP 621
    for entry in data.get("project", {}).get("dependencies", []) or []:
        if isinstance(entry, str) and (match := _REQUIREMENT_RE.match(entry.strip())):
            deps[match.group("name")] = (match.group("spec") or "").strip() or "*"
    optional = data.get("project", {}).get("optional-dependencies", {}) or {}
    for group in optional.values():
        for entry in group or []:
            if isinstance(entry, str) and (match := _REQUIREMENT_RE.match(entry.strip())):
                deps[match.group("name")] = (match.group("spec") or "").strip() or "*"

    # Poetry
    poetry = data.get("tool", {}).get("poetry", {})
    for block in ("dependencies", "dev-dependencies"):
        for name, spec in (poetry.get(block) or {}).items():
            if name.lower() == "python":
                continue
            deps[str(name)] = spec if isinstance(spec, str) else json.dumps(spec)

    return ParsedManifest(ecosystem=Ecosystem.PYPI, dependencies=deps)


# ------------------------------------------------------------------------- NuGet


def _parse_csproj(content: str) -> ParsedManifest:
    root = ElementTree.fromstring(content)
    deps: dict[str, str] = {}
    for element in root.iter():
        if _strip_ns(element.tag) != "PackageReference":
            continue
        name = element.get("Include") or element.get("Update")
        if not name:
            continue
        version = element.get("Version")
        if version is None:
            # Version can also be a child element.
            child = next((c for c in element if _strip_ns(c.tag) == "Version"), None)
            version = (child.text or "").strip() if child is not None else None
        deps[name] = version or "*"
    return ParsedManifest(ecosystem=Ecosystem.NUGET, dependencies=deps)


def _parse_packages_config(content: str) -> ParsedManifest:
    root = ElementTree.fromstring(content)
    deps: dict[str, str] = {}
    for element in root.iter():
        if _strip_ns(element.tag) != "package":
            continue
        name = element.get("id")
        if name:
            deps[name] = element.get("version") or "*"
    return ParsedManifest(ecosystem=Ecosystem.NUGET, dependencies=deps)


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


# ---------------------------------------------------------------------- Composer


def _parse_composer_json(content: str) -> ParsedManifest:
    data = json.loads(content)
    deps: dict[str, str] = {}
    for block in ("require", "require-dev"):
        section = data.get(block)
        if isinstance(section, dict):
            deps.update(
                {
                    str(k): str(v)
                    for k, v in section.items()
                    # "php" and "ext-*" are platform constraints, not packages.
                    if k != "php" and not str(k).startswith("ext-")
                }
            )
    return ParsedManifest(ecosystem=Ecosystem.COMPOSER, dependencies=deps)


def _parse_composer_lock(content: str) -> ParsedManifest:
    data = json.loads(content)
    deps: dict[str, str] = {}
    for block in ("packages", "packages-dev"):
        for entry in data.get(block) or []:
            if isinstance(entry, dict) and entry.get("name"):
                deps[str(entry["name"])] = str(entry.get("version", "*"))
    return ParsedManifest(
        ecosystem=Ecosystem.COMPOSER, dependencies=deps, is_lockfile=True
    )


# ---------------------------------------------------------------------- internals

_EXACT_MATCHES: dict[str, Ecosystem] = {
    "package.json": Ecosystem.NPM,
    "package-lock.json": Ecosystem.NPM,
    "npm-shrinkwrap.json": Ecosystem.NPM,
    "pyproject.toml": Ecosystem.PYPI,
    "packages.config": Ecosystem.NUGET,
    "composer.json": Ecosystem.COMPOSER,
    "composer.lock": Ecosystem.COMPOSER,
}

_VENDOR_DIRS = frozenset(
    {"node_modules", "vendor", "bower_components", "site-packages", ".venv", "venv"}
)

_REQUIREMENTS_RE = re.compile(r"^requirements.*\.txt$", re.IGNORECASE)

# name[extras] followed by an optional version specifier / URL.
_REQUIREMENT_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?:\[[^\]]*\])?"
    r"\s*(?P<spec>[^;]*)?"
)

_VERSION_CORE_RE = re.compile(r"\d+(?:\.\d+)*")


def _is_vendored(path: str) -> bool:
    return any(part in _VENDOR_DIRS for part in PurePosixPath(path).parts)

"""Sentinel-AI command line entry point.

Invoked by a Husky `pre-commit` hook with no arguments, in which case it scans
the staged index and exits 0 (allow) or 1 (block).

argparse is used rather than a CLI framework on purpose: this process starts on
every commit, and third-party import cost is latency a developer feels.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__
from .ai import AIClient, AIUnavailable
from .config import (
    ConfigError,
    Settings,
    open_host_config_in_editor,
    resolved_config_paths,
)
from .decision_engine import EXIT_BLOCK, EXIT_ERROR, EXIT_PASS, decide, requires_ai_review
from .gitdiff import GitError, repo_root
from .manifests import ParsedManifest
from .models import AIVerdict, ScanResult, Severity
from .reporting import Reporter, to_json
from .scanner import Scanner, SourceRevision, trivy_version


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sentinel-ai",
        description=(
            "Block malicious, typo-squatted, or vulnerable dependencies "
            "before they reach a commit."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"sentinel-ai {__version__}"
    )

    subparsers = parser.add_subparsers(dest="command")

    check = subparsers.add_parser("check", help="scan a change set (default)")
    _add_check_arguments(check)

    doctor = subparsers.add_parser(
        "doctor", help="verify Trivy and the on-prem model server"
    )
    doctor.add_argument(
        "--repo", type=Path, default=None, help="repository root (default: cwd)"
    )
    doctor.add_argument("--no-color", action="store_true")

    install = subparsers.add_parser(
        "install-hook",
        help="append sentinel-ai to an existing Husky pre-commit hook",
    )
    install.add_argument(
        "--repo", type=Path, default=None, help="target repository (default: cwd)"
    )

    config = subparsers.add_parser(
        "config", help="show or edit the active organisation configuration"
    )
    config.add_argument(
        "--json", action="store_true", help="emit machine-readable output"
    )
    config.add_argument("--no-color", action="store_true")
    config_sub = config.add_subparsers(dest="config_command")
    config_edit = config_sub.add_parser(
        "edit", help="open host config in the default editor"
    )
    config_edit.add_argument("--no-color", action="store_true")

    update = subparsers.add_parser(
        "update", help="upgrade the installed CLI from the latest GitHub release"
    )
    update.add_argument(
        "--from",
        dest="source",
        type=Path,
        default=None,
        help="local checkout instead of the latest release",
    )
    update.add_argument("--no-color", action="store_true")

    # Allow bare `sentinel-ai` with check flags, so the hook needs no subcommand.
    _add_check_arguments(parser)
    return parser


def _add_check_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repo", type=Path, default=None, help="repository root (default: cwd)"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="scan every manifest on disk instead of just staged changes",
    )
    parser.add_argument(
        "--range",
        metavar="BASE..HEAD",
        default=None,
        help="scan changes between two refs instead of the index",
    )
    parser.add_argument("--json", action="store_true", help="emit a JSON report")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument(
        "--no-ai", action="store_true", help="skip the AI review stage entirely"
    )
    parser.add_argument("--no-trivy", action="store_true", help="skip CVE scanning")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="block the commit when Sentinel-AI or the model server fails",
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        return _doctor(args)
    if args.command == "install-hook":
        return _install_hook(args)
    if args.command == "config":
        if getattr(args, "config_command", None) == "edit":
            return _config_edit(args)
        return _config(args)
    if args.command == "update":
        return _update(args)
    return _check(args)


def _check(args: argparse.Namespace) -> int:
    reporter = Reporter(verbose=args.verbose, no_color=args.no_color)
    started = time.perf_counter()

    try:
        root = repo_root(args.repo) if args.repo else repo_root()
    except GitError as exc:
        reporter.error(str(exc))
        return EXIT_ERROR if args.strict else EXIT_PASS

    try:
        settings = Settings.load()
    except ConfigError as exc:
        reporter.error(str(exc))
        return EXIT_ERROR

    settings.repo_root = root
    settings.verbose = settings.verbose or args.verbose
    if args.no_ai:
        settings.ai.enabled = False
    if args.no_trivy:
        settings.trivy.enabled = False
    if args.strict:
        settings.ai.fail_open = False

    revision = _revision_from(args)

    scanner = Scanner(settings)
    try:
        scan = scanner.scan(revision)
    except GitError as exc:
        reporter.error(str(exc))
        return EXIT_BLOCK if args.strict else EXIT_PASS
    except Exception as exc:
        # Broad by design: any scanner bug must produce a deliberate decision
        # (see _handle_internal_error), never an unhandled traceback in a hook.
        return _handle_internal_error(reporter, exc, strict=args.strict)

    reporter.scan_summary(scan)

    # Findings can exist with no dependency change at all — an edited
    # `postinstall` hook is the obvious case — so both must be empty to
    # short-circuit here.
    if not scan.changes and not scan.findings:
        if args.json:
            decision = decide(scan, None, settings.policy)
            print(to_json(decision, scan))
        elif args.verbose:
            reporter.success("Sentinel-AI: no dependency changes")
        return EXIT_PASS

    verdict = _maybe_analyse(scan, scanner.parsed_manifests, settings, reporter)
    decision = decide(scan, verdict, settings.policy)

    if args.json:
        print(to_json(decision, scan))
    else:
        reporter.degraded(scan.degraded_reasons)
        reporter.render(decision, scan)
        if args.verbose:
            elapsed = time.perf_counter() - started
            reporter.info(f"[dim]completed in {elapsed:.2f}s[/dim]")

    return decision.exit_code


def _maybe_analyse(
    scan: ScanResult,
    parsed_manifests: dict[str, ParsedManifest],
    settings: Settings,
    reporter: Reporter,
) -> AIVerdict | None:
    """Run the AI review stage when it is enabled and the triage gate says it helps."""
    if not settings.ai.enabled:
        return None
    if not requires_ai_review(scan, settings.policy):
        return None

    try:
        return AIClient(settings.ai).analyse(
            scan.changes, scan.findings, parsed_manifests
        )
    except AIUnavailable as exc:
        if settings.ai.fail_open:
            scan.degraded_reasons.append(
                f"AI review skipped — {exc}. Deterministic checks still ran."
            )
            return None
        # fail-closed: surface it as a blocking verdict rather than a silent pass.
        return AIVerdict(
            risk_level=Severity.HIGH,
            confidence=1.0,
            summary=f"AI review could not complete and strict mode is on: {exc}",
            recommended_action=(
                "Start the model server, or re-run with --no-ai if the outage is expected."
            ),
        )


def _revision_from(args: argparse.Namespace) -> SourceRevision:
    if args.all:
        return SourceRevision(mode="worktree")
    if args.range:
        base, _, head = args.range.partition("..")
        return SourceRevision(
            mode="range", base_ref=base or "HEAD~1", head_ref=head or "HEAD"
        )
    return SourceRevision(mode="staged")


def _handle_internal_error(reporter: Reporter, exc: Exception, *, strict: bool) -> int:
    """A Sentinel-AI bug should not hold every developer's commits hostage.

    Default is fail-open with a loud warning; `--strict` inverts that for
    environments where an unverified commit is the worse outcome.
    """
    reporter.error(f"{type(exc).__name__}: {exc}")
    if strict:
        reporter.warn("strict mode: blocking the commit")
        return EXIT_BLOCK
    reporter.warn("allowing the commit — dependencies were NOT verified")
    return EXIT_PASS


def _doctor(args: argparse.Namespace) -> int:
    reporter = Reporter(verbose=True, no_color=getattr(args, "no_color", False))
    try:
        root = repo_root(getattr(args, "repo", None))
    except GitError:
        root = Path.cwd()
        reporter.warn(f"not in a git repository; using {root}")

    settings = Settings.load()
    reporter.info(f"[bold]Sentinel-AI {__version__}[/bold]")
    reporter.info(f"  repo:   {root}")
    reporter.info(
        f"  policy: block at or above {settings.policy.block_at_or_above.value}"
    )

    version = trivy_version(settings)
    if version:
        reporter.info(f"  trivy:  [green]{version}[/green]")
    else:
        reporter.info(
            f"  trivy:  [yellow]not found[/yellow] "
            f"(looked for `{settings.trivy.binary_path}`)"
        )

    if not settings.ai.enabled:
        reporter.info("  ai:     [dim]disabled in config[/dim]")
        return EXIT_PASS

    try:
        status = AIClient(settings.ai).health_check()
        reporter.info(f"  ai:     [green]{status}[/green] at {settings.ai.base_url}")
    except AIUnavailable as exc:
        reporter.info(f"  ai:     [red]unavailable[/red] — {exc}")
        return EXIT_BLOCK

    return EXIT_PASS


def _config(args: argparse.Namespace) -> int:
    reporter = Reporter(verbose=True, no_color=getattr(args, "no_color", False))

    try:
        settings = Settings.load()
    except ConfigError as exc:
        reporter.error(str(exc))
        return EXIT_ERROR

    sources = resolved_config_paths()
    payload = {
        "sources": [str(path) for path in sources],
        "policy": {
            "block_at_or_above": settings.policy.block_at_or_above.value,
            "block_on_install_scripts": settings.policy.block_on_install_scripts,
            "block_on_nonregistry_source": settings.policy.block_on_nonregistry_source,
            "allowlist": settings.policy.allowlist,
            "denylist": settings.policy.denylist,
        },
        "ai": {
            "enabled": settings.ai.enabled,
            "base_url": settings.ai.base_url,
            "model": settings.ai.model,
            "max_output_tokens": settings.ai.max_output_tokens,
            "fail_open": settings.ai.fail_open,
        },
        "trivy": {
            "enabled": settings.trivy.enabled,
            "binary_path": settings.trivy.binary_path,
        },
    }

    if args.json:
        print(json.dumps(payload, indent=2))
        return EXIT_PASS

    reporter.info(f"[bold]Sentinel-AI {__version__}[/bold]")
    reporter.info("  configuration sources:")
    if sources:
        for path in sources:
            reporter.info(f"    [cyan]{path}[/cyan]")
    else:
        reporter.warn("    no config file found — using built-in defaults")

    reporter.info(
        f"  policy: block at or above {settings.policy.block_at_or_above.value}"
    )
    if settings.policy.allowlist:
        reporter.info(f"          allowlist: {', '.join(settings.policy.allowlist)}")
    if settings.policy.denylist:
        reporter.info(f"          denylist: {', '.join(settings.policy.denylist)}")

    if settings.ai.enabled:
        reporter.info(f"  ai:     {settings.ai.base_url}")
        reporter.info(f"          model {settings.ai.model}")
    else:
        reporter.info("  ai:     [dim]disabled[/dim]")

    reporter.info(f"  trivy:  `{settings.trivy.binary_path}`")
    reporter.info("[dim]Edit host overrides: sentinel-ai config edit[/dim]")
    return EXIT_PASS


def _config_edit(args: argparse.Namespace) -> int:
    reporter = Reporter(verbose=True, no_color=getattr(args, "no_color", False))
    try:
        path = open_host_config_in_editor()
    except ConfigError as exc:
        reporter.error(str(exc))
        return EXIT_ERROR

    reporter.success(f"opened {path}")
    return EXIT_PASS


def _update(args: argparse.Namespace) -> int:
    from .update import UpdateError, run_update

    reporter = Reporter(verbose=True, no_color=getattr(args, "no_color", False))
    try:
        ref = run_update(source=args.source)
    except UpdateError as exc:
        reporter.error(str(exc))
        return EXIT_ERROR

    reporter.success(f"updated to {ref}")
    reporter.info(
        "[dim]Open a new terminal if `sentinel-ai --version` still shows the old build.[/dim]"
    )
    return EXIT_PASS


_HOOK_LINE = "sentinel-ai check || exit 1"


def _install_hook(args: argparse.Namespace) -> int:
    reporter = Reporter(verbose=True)
    try:
        root = repo_root(args.repo)
    except GitError as exc:
        reporter.error(str(exc))
        return EXIT_ERROR

    husky_dir = root / ".husky"
    if not husky_dir.is_dir():
        reporter.error(
            f"{husky_dir} does not exist. Run `npx husky init` in the repository first."
        )
        return EXIT_ERROR

    hook_path = husky_dir / "pre-commit"
    if hook_path.exists():
        existing = hook_path.read_text(encoding="utf-8")
        if "sentinel-ai" in existing:
            reporter.success(f"sentinel-ai hook already present in {hook_path}")
            return EXIT_PASS

        hook_path.write_text(
            _append_hook_line(existing),
            encoding="utf-8",
            newline="\n",
        )
        hook_path.chmod(0o755)
        reporter.success(f"appended sentinel-ai hook to {hook_path}")
        return EXIT_PASS

    hook_path.write_text(f"{_HOOK_LINE}\n", encoding="utf-8", newline="\n")
    hook_path.chmod(0o755)
    reporter.success(f"wrote {hook_path}")
    return EXIT_PASS


def _append_hook_line(content: str) -> str:
    trimmed = content.rstrip("\n")
    if not trimmed:
        return f"{_HOOK_LINE}\n"
    return f"{trimmed}\n{_HOOK_LINE}\n"


if __name__ == "__main__":
    sys.exit(main())

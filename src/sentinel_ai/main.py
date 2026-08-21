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
from typing import TYPE_CHECKING

from . import __version__
from .ai import AIUnavailable, health_check
from .config import (
    ConfigError,
    Settings,
    host_config_path,
    open_host_config_in_editor,
    resolved_config_paths,
)
from .decision_engine import EXIT_BLOCK, EXIT_ERROR, EXIT_PASS, decide
from .gitdiff import GitError, repo_root
from .reporting import Reporter, to_json
from .scanner import Scanner, SourceRevision, trivy_version

if TYPE_CHECKING:
    from .diff_review.engine import DiffReviewOutcome


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

    diff_review = subparsers.add_parser(
        "diff-review",
        help="review the staged code diff with the on-prem model",
    )
    _add_diff_review_arguments(diff_review)

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

    install_global = subparsers.add_parser(
        "install-global-hook",
        help="cover every repository on this machine with one git hook",
    )
    install_global.add_argument(
        "--org",
        dest="orgs",
        action="append",
        default=None,
        metavar="NAME",
        help="only run in repos whose origin matches NAME (repeatable; "
        "defaults to hook.organizations in config)",
    )
    install_global.add_argument(
        "--all",
        dest="all_repos",
        action="store_true",
        help="run in every repository on this machine, including personal ones",
    )
    install_global.add_argument(
        "--force", action="store_true", help="replace an existing core.hooksPath"
    )
    install_global.add_argument("--no-color", action="store_true")

    uninstall_global = subparsers.add_parser(
        "uninstall-global-hook",
        help="remove the machine-wide git hook",
    )
    uninstall_global.add_argument("--no-color", action="store_true")

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

    uninstall = subparsers.add_parser(
        "uninstall", help="remove sentinel-ai and ~/.sentinel-ai from this host"
    )
    uninstall.add_argument(
        "--yes",
        action="store_true",
        help="confirm removal of host data and the uv tool install",
    )
    uninstall.add_argument("--no-color", action="store_true")

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


def _add_diff_review_arguments(parser: argparse.ArgumentParser) -> None:
    """Flags only — no positional argument.

    Phase 1 adds a `stats` subcommand under `diff-review`, and a positional
    would be ambiguous with it. There is no commit-message input: this layer
    runs inside `check` on the pre-commit hook, where no message exists yet.
    """
    parser.add_argument(
        "--repo", type=Path, default=None, help="repository root (default: cwd)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be sent; no model call, no log entry",
    )
    parser.add_argument(
        "--no-ai", action="store_true", help="run the pipeline without the model"
    )
    parser.add_argument("--json", action="store_true", help="emit a JSON report")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="block the commit when the model server is unreachable",
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "diff-review":
        # Lazy: the check path must not pay for these imports (AGENTS.md rule 4).
        from .diff_review.engine import run_diff_review

        return run_diff_review(args)
    if args.command == "doctor":
        return _doctor(args)
    if args.command == "install-hook":
        return _install_hook(args)
    if args.command == "install-global-hook":
        return _install_global_hook(args)
    if args.command == "uninstall-global-hook":
        return _uninstall_global_hook(args)
    if args.command == "config":
        if getattr(args, "config_command", None) == "edit":
            return _config_edit(args)
        return _config(args)
    if args.command == "update":
        return _update(args)
    if args.command == "uninstall":
        return _uninstall(args)
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

    # Runs on the same hook line as the dependency check, so a CLI update is
    # all it takes to reach a machine. It must happen before the short-circuit
    # below: a commit with no dependency change at all is the common case, and
    # it is exactly the commit this layer exists to look at.
    diff_outcome = _maybe_review_diff(args, root, settings, reporter, revision)

    # Findings can exist with no dependency change at all — an edited
    # `postinstall` hook is the obvious case — so both must be empty to
    # short-circuit here.
    if not scan.changes and not scan.findings:
        if args.json:
            decision = decide(scan, settings.policy)
            print(to_json(decision, scan))
        elif args.verbose:
            reporter.success("Sentinel-AI: no dependency changes")
        return _with_diff_review(EXIT_PASS, diff_outcome)

    decision = decide(scan, settings.policy)

    if args.json:
        print(to_json(decision, scan))
    else:
        reporter.degraded(scan.degraded_reasons)
        reporter.render(decision, scan)
        if args.verbose:
            elapsed = time.perf_counter() - started
            reporter.info(f"[dim]completed in {elapsed:.2f}s[/dim]")

    return _with_diff_review(decision.exit_code, diff_outcome)


def _maybe_review_diff(
    args: argparse.Namespace,
    root: Path,
    settings: Settings,
    reporter: Reporter,
    revision: SourceRevision,
) -> DiffReviewOutcome | None:
    """Review the staged code diff, or return None when it does not apply.

    Only in `staged` mode: `--all` and `--range` answer a different question,
    and this layer reads the index.
    """
    if revision.mode != "staged":
        return None

    # Lazy: a commit that never reaches this line should not pay for the import.
    from .diff_review.engine import report, review_staged

    try:
        outcome = review_staged(root, settings, no_ai=args.no_ai, strict=args.strict)
    except Exception as exc:
        # Broad by design, same reasoning as the scanner: this now sits on
        # every commit for the whole team, so a bug here must cost a warning,
        # never someone's ability to commit.
        reporter.warn(f"diff review skipped after an internal error: {exc}")
        return None

    report(reporter, outcome)
    return outcome


def _with_diff_review(exit_code: int, outcome: DiffReviewOutcome | None) -> int:
    """Let a blocking diff review fail an otherwise-passing check."""
    if outcome is not None and outcome.blocks and exit_code == EXIT_PASS:
        return EXIT_BLOCK
    return exit_code


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

    _report_global_hook(reporter, settings)

    if not settings.ai.enabled:
        reporter.info("  ai:     [dim]disabled in config[/dim]")
        return EXIT_PASS

    try:
        status = health_check(settings.ai)
        reporter.info(f"  ai:     [green]{status}[/green] at {settings.ai.base_url}")
    except AIUnavailable as exc:
        reporter.info(f"  ai:     [red]unavailable[/red] — {exc}")
        return EXIT_BLOCK

    return EXIT_PASS


def _report_global_hook(reporter: Reporter, settings: Settings) -> None:
    from .globalhook import status

    state = status()
    if state.installed:
        orgs = state.installed_organizations or []
        scope = ", ".join(orgs) if orgs else "every repository"
        reporter.info(f"  hook:   [green]global[/green] — {scope}")
        if state.drifted_from(settings.hook.organizations):
            # Editing the config looks like it should take effect on the next
            # commit; nothing in the commit output would say otherwise.
            reporter.warn(
                "  hook.organizations has changed since the hook was written — "
                "re-run `sentinel-ai install-global-hook` to apply it"
            )
    elif state.points_elsewhere:
        reporter.info(f"  hook:   [dim]core.hooksPath -> {state.hooks_path}[/dim]")
    else:
        reporter.info(
            "  hook:   [yellow]not installed globally[/yellow] "
            "(run `sentinel-ai install-global-hook`)"
        )


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
        },
        "diff_review": {
            "enabled": settings.diff_review.enabled,
            "max_output_tokens": settings.diff_review.max_output_tokens,
            "timeout_seconds": settings.diff_review.timeout_seconds,
            "fail_open": settings.diff_review.fail_open,
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


def _uninstall(args: argparse.Namespace) -> int:
    from .uninstall import UninstallError, run_uninstall, uninstall_targets

    reporter = Reporter(verbose=True, no_color=getattr(args, "no_color", False))
    if not args.yes:
        reporter.warn("This will remove:")
        for target in uninstall_targets():
            reporter.info(f"  - {target}")
        reporter.warn("Re-run with --yes to confirm.")
        reporter.info("[dim]Husky hooks in project repos are not modified.[/dim]")
        return EXIT_ERROR

    try:
        removed = run_uninstall(yes=True)
    except UninstallError as exc:
        reporter.error(str(exc))
        return EXIT_ERROR

    if removed:
        for item in removed:
            reporter.success(f"removed {item}")
    else:
        reporter.info("nothing to remove — sentinel-ai is not installed on this host")
    reporter.info("[dim]Husky hooks in project repos were not modified.[/dim]")
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


def _install_global_hook(args: argparse.Namespace) -> int:
    from .globalhook import GlobalHookError, install

    reporter = Reporter(verbose=True, no_color=getattr(args, "no_color", False))
    try:
        settings = Settings.load()
    except ConfigError as exc:
        reporter.error(str(exc))
        return EXIT_ERROR

    if args.all_repos and args.orgs:
        reporter.error("--all and --org cannot be combined")
        return EXIT_ERROR

    organizations = [] if args.all_repos else (args.orgs or settings.hook.organizations)
    if not organizations and not args.all_repos:
        reporter.error(
            "no organisations configured — set hook.organizations in "
            f"{host_config_path()}, pass --org NAME, or use --all"
        )
        return EXIT_ERROR

    try:
        path = install(organizations, force=args.force)
    except (GlobalHookError, GitError) as exc:
        reporter.error(str(exc))
        return EXIT_ERROR

    reporter.success(f"wrote {path}")
    if organizations:
        reporter.info(f"  runs in repos matching: {', '.join(organizations)}")
    else:
        reporter.warn("  runs in EVERY repository on this machine")
    reporter.info(
        "[dim]Repos with their own core.hooksPath (Husky) are unaffected.[/dim]"
    )
    return EXIT_PASS


def _uninstall_global_hook(args: argparse.Namespace) -> int:
    from .globalhook import uninstall

    reporter = Reporter(verbose=True, no_color=getattr(args, "no_color", False))
    try:
        removed = uninstall()
    except GitError as exc:
        reporter.error(str(exc))
        return EXIT_ERROR

    if not removed:
        reporter.info("no machine-wide hook was installed")
        return EXIT_PASS
    for item in removed:
        reporter.success(f"removed {item}")
    return EXIT_PASS


def _append_hook_line(content: str) -> str:
    trimmed = content.rstrip("\n")
    if not trimmed:
        return f"{_HOOK_LINE}\n"
    return f"{trimmed}\n{_HOOK_LINE}\n"


if __name__ == "__main__":
    sys.exit(main())

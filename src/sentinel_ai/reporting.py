"""Terminal output.

A blocked commit is an interruption, so the report has to earn it: say what
was found, in which package, and what to do about it — without making the
developer go read the source to find out.

Two constraints shape this module:

* Windows consoles still default to a legacy code page. Findings carry text
  from CVE advisories and the LLM, which is arbitrary Unicode, so everything
  is transliterated at the output boundary rather than at each call site.
* Finding detail can quote attacker-controlled script text. It is printed as
  literal `Text`, never parsed as Rich markup.
"""

from __future__ import annotations

import json
import sys
import textwrap

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from .config import host_config_path
from .models import Decision, Finding, FindingSource, ScanResult, Severity

_SEVERITY_STYLE: dict[Severity, str] = {
    Severity.CRITICAL: "bold white on red",
    Severity.HIGH: "bold red",
    Severity.MEDIUM: "yellow",
    Severity.LOW: "cyan",
    Severity.NONE: "dim",
}

_SOURCE_LABEL: dict[FindingSource, str] = {
    FindingSource.TRIVY: "trivy",
    FindingSource.HEURISTIC: "heuristic",
    FindingSource.POLICY: "policy",
}

_UNICODE_GLYPHS = {"arrow": "→", "bullet": "•", "tick": "✓"}
_ASCII_GLYPHS = {"arrow": "->", "bullet": "*", "tick": "OK"}

# Punctuation that shows up constantly in advisory text and model output.
# Mapped explicitly so a legacy console renders it readably instead of "?".
_TRANSLITERATIONS = str.maketrans(
    {
        "—": "-",  # em dash
        "–": "-",  # en dash
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "…": "...",
        "→": "->",
        "•": "*",
        "✓": "OK",
        " ": " ",  # non-breaking space
    }
)


class Reporter:
    def __init__(self, *, verbose: bool = False, no_color: bool = False) -> None:
        # stderr keeps the report visible even when a caller pipes stdout.
        self.console = Console(stderr=True, no_color=no_color, soft_wrap=False)
        self.verbose = verbose
        self.ascii_only = _needs_ascii()
        self.glyphs = _ASCII_GLYPHS if self.ascii_only else _UNICODE_GLYPHS

    # ---------------------------------------------------------------- output

    def sanitise(self, text: str) -> str:
        """Make `text` safe for this console's encoding.

        Applied to every printed string. Findings embed CVE descriptions and
        LLM output, so assuming ASCII input anywhere upstream would be wrong.
        """
        if not self.ascii_only:
            return text
        translated = text.translate(_TRANSLITERATIONS)
        encoding = getattr(sys.stderr, "encoding", None) or "ascii"
        return translated.encode(encoding, errors="replace").decode(
            encoding, errors="replace"
        )

    def _print(self, markup: str = "") -> None:
        """Print Rich markup, sanitised."""
        self.console.print(self.sanitise(markup))

    def _print_literal(self, text: str) -> None:
        """Print untrusted text with markup parsing disabled."""
        self.console.print(Text(self.sanitise(text)))

    def _wrap(self, text: str, initial: str = "  ", subsequent: str = "  ") -> list[str]:
        """Wrap to console width, keeping continuation lines indented.

        Rich re-wraps at column 0, which visually detaches a finding's detail
        from its heading. Wrapping here keeps each block as one visual unit.
        """
        width = max(48, self.console.width - 1)
        lines: list[str] = []
        for index, paragraph in enumerate(text.splitlines()):
            if not paragraph.strip():
                continue
            wrapped = textwrap.wrap(
                paragraph,
                width=width,
                initial_indent=initial if index == 0 else subsequent,
                subsequent_indent=subsequent,
                break_long_words=False,
                break_on_hyphens=False,
            )
            lines.extend(wrapped or [f"{subsequent}{paragraph}"])
        return lines

    # --------------------------------------------------------------- surface

    def scan_summary(self, scan: ScanResult) -> None:
        if not self.verbose:
            return
        if scan.scanned_manifests:
            self._print(
                f"[dim]Scanned {len(scan.scanned_manifests)} manifest(s): "
                f"{', '.join(scan.scanned_manifests)}[/dim]"
            )
        self._print(f"[dim]{len(scan.changes)} dependency change(s) detected[/dim]")

    def degraded(self, reasons: list[str]) -> None:
        """Coverage gaps. Always shown — silent partial scans are dangerous."""
        for reason in reasons:
            for line in self._wrap(reason, "! ", "  "):
                self._print(f"[yellow]{line}[/yellow]")

    def render(self, decision: Decision, scan: ScanResult) -> None:
        if decision.blocked:
            self._render_blocked(decision)
        else:
            self._render_passed(decision, scan)

    def error(self, message: str) -> None:
        self._print(f"[bold red]Sentinel-AI error:[/bold red] {message}")

    def warn(self, message: str) -> None:
        self._print(f"[yellow]![/yellow] {message}")

    def info(self, message: str) -> None:
        self._print(message)

    def success(self, message: str) -> None:
        self._print(f"[green]{self.glyphs['tick']}[/green] {message}")

    # ------------------------------------------------------------- internals

    def _render_blocked(self, decision: Decision) -> None:
        self.console.print()
        self.console.print(
            Panel(
                Text.from_markup(
                    self.sanitise(
                        f"[bold red]Commit blocked by Sentinel-AI[/bold red]\n"
                        f"[dim]{decision.reason}[/dim]"
                    )
                ),
                border_style="red",
                expand=False,
            )
        )

        for finding in decision.findings:
            self._print_finding(finding)

        if decision.warnings:
            if self.verbose:
                for finding in decision.warnings:
                    self._print_finding(finding, dim=True)
            else:
                self._print(
                    f"\n[dim]Plus {len(decision.warnings)} lower-severity "
                    f"warning(s). Re-run with --verbose to see them.[/dim]"
                )

        bullet = self.glyphs["bullet"]
        self.console.print()
        self._print("[bold]To proceed:[/bold]")
        self._print(f"  {bullet} Fix the findings above, then re-stage and commit.")
        self._print(
            f"  {bullet} False positive? Add the package to [cyan]allowlist[/cyan]"
        )
        self._print(f"    in [cyan]{host_config_path()}[/cyan] (`sentinel-ai config`).")
        self._print(
            f"  {bullet} Bypass once (audited, discouraged): "
            f"[cyan]git commit --no-verify[/cyan]"
        )
        self.console.print()

    def _render_passed(self, decision: Decision, scan: ScanResult) -> None:
        for finding in decision.warnings:
            self._print_finding(finding, dim=True)
        if decision.warnings or self.verbose or scan.changes:
            self.success(f"Sentinel-AI: {decision.reason}")

    def _print_finding(self, finding: Finding, *, dim: bool = False) -> None:
        style = "dim" if dim else _SEVERITY_STYLE[finding.severity]
        label = finding.severity.value.upper()
        source = _SOURCE_LABEL[finding.source]

        self.console.print()
        self._print(
            f"[{style}] {label} [/{style}] "
            f"[bold]{finding.title}[/bold] [dim]({source})[/dim]"
        )
        # A coordinate is one unbreakable token; textwrap would strand the
        # label on its own line, so Rich folds it instead.
        self._print_literal(f"  package: {finding.package}")

        for line in self._wrap(finding.detail.strip()):
            self._print_literal(line)

        if finding.remediation:
            arrow = self.glyphs["arrow"]
            indent = " " * (len(arrow) + 3)
            for line in self._wrap(finding.remediation, f"  {arrow} ", indent):
                self.console.print(Text(self.sanitise(line), style="green"))
        if finding.reference:
            self._print_literal(f"  {finding.reference}")


def _needs_ascii() -> bool:
    """True when stderr cannot represent the arrow/bullet/tick glyphs."""
    encoding = getattr(sys.stderr, "encoding", None)
    if not encoding:
        return True
    try:
        "".join(_UNICODE_GLYPHS.values()).encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return True
    return False


def to_json(decision: Decision, scan: ScanResult) -> str:
    """Machine-readable report for CI dashboards and agent consumption.

    `ensure_ascii` keeps this safe to print on any console, unlike the
    human-facing report which is transliterated instead.
    """
    return json.dumps(
        {
            "blocked": decision.blocked,
            "exit_code": decision.exit_code,
            "reason": decision.reason,
            "scanned_manifests": scan.scanned_manifests,
            "changes": [change.model_dump(mode="json") for change in scan.changes],
            "findings": [f.model_dump(mode="json") for f in decision.findings],
            "warnings": [f.model_dump(mode="json") for f in decision.warnings],
            "degraded": scan.degraded_reasons,
        },
        indent=2,
        ensure_ascii=True,
    )

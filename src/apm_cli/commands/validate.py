"""``apm validate`` -- offline conformance check for a project's manifest.

Thin Click wrapper around :mod:`apm_cli.validation`. All verdict logic lives in
the engine; this module only resolves what to validate and renders the report.

The command is read-only: it writes nothing, creates no user config, and makes
no network calls, so it is safe in CI, in a pre-commit hook, or against a
directory the caller does not own.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ..constants import APM_YML_FILENAME
from ..core.command_logger import CommandLogger
from ..validation import CHECK_FAMILIES, Severity, ValidationReport, run_validation


@click.command(
    help=(
        "Validate apm.yml and the project around it, offline. Checks manifest "
        "schema conformance, project structure, declared local paths, and "
        "lockfile drift. Exits non-zero when an error is found, or -- with "
        "--strict -- when any warning is found."
    )
)
@click.argument(
    "path",
    required=False,
    type=click.Path(exists=True, path_type=Path),
)
@click.option(
    "--strict",
    is_flag=True,
    help="Treat warnings as failures. Use this to gate CI.",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
    help="Report format.",
)
@click.option(
    "--check",
    "check_families",
    multiple=True,
    type=click.Choice(CHECK_FAMILIES),
    help="Limit to one check family; repeatable. Default: all families.",
)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def validate(
    path: Path | None,
    strict: bool,
    output_format: str,
    check_families: tuple[str, ...],
    verbose: bool,
) -> None:
    """Validate a project's manifest and surroundings."""
    project_root, manifest_path = _resolve_target(path)

    report = run_validation(
        project_root,
        manifest_path=manifest_path,
        checks=check_families or None,
    )

    if output_format == "json":
        click.echo(report.to_json(strict=strict))
    else:
        _render_text(report, strict=strict, verbose=verbose)

    exit_code = report.exit_code(strict=strict)
    if exit_code != 0:
        sys.exit(exit_code)


def _resolve_target(path: Path | None) -> tuple[Path, Path]:
    """Resolve ``(project_root, manifest_path)`` from the optional argument.

    Accepts a project directory, a manifest file directly, or nothing -- in
    which case the manifest is discovered by walking up from the working
    directory, the same ergonomic every other project-scoped command has.
    """
    if path is not None:
        if path.is_file():
            return path.parent.resolve(), path.resolve()
        root = path.resolve()
        return root, root / APM_YML_FILENAME

    from ._helpers import _find_apm_yml

    discovered = _find_apm_yml()
    if discovered is not None:
        return discovered.parent, discovered

    cwd = Path.cwd()
    return cwd, cwd / APM_YML_FILENAME


def _render_text(report: ValidationReport, *, strict: bool, verbose: bool) -> None:
    """Print the human-readable report."""
    logger = CommandLogger("validate", verbose=verbose)

    identity = f" ({report.name} {report.version})" if report.name else ""
    logger.start(f"Validating {report.manifest_path}{identity}")
    logger.verbose_detail(f"Checks: {', '.join(report.checks_run)}")

    for finding in report.findings:
        location = f" [{finding.field}]" if finding.field else ""
        line = f"{finding.code}{location}: {finding.message}"
        if finding.severity is Severity.ERROR:
            logger.error(line)
        else:
            logger.warning(line)
        if finding.hint:
            click.echo(f"      {finding.hint}")

    _render_summary(report, logger, strict=strict)


def _render_summary(report: ValidationReport, logger: CommandLogger, *, strict: bool) -> None:
    """Print the one-line verdict."""
    errors = len(report.errors)
    warnings = len(report.warnings)

    if errors:
        logger.blank_line()
        logger.error(f"Validation failed: {_count(errors, 'error')}, {_count(warnings, 'warning')}")
        return

    if warnings and strict:
        logger.blank_line()
        logger.error(f"Validation failed under --strict: {_count(warnings, 'warning')}")
        return

    if warnings:
        logger.blank_line()
        logger.success(f"Validation passed, with {_count(warnings, 'warning')}", symbol="check")
        logger.info("Run with --strict to fail on warnings.")
        return

    logger.success("Validation passed; no issues found.", symbol="check")


def _count(value: int, noun: str) -> str:
    """Pluralize a count for the summary line."""
    return f"{value} {noun}" if value == 1 else f"{value} {noun}s"

"""Orchestration for ``apm validate``.

Holds no output logic and no Click dependency: the runner produces a
:class:`ValidationReport` and the command layer decides how to render it. That
keeps the engine callable from a future ``apm doctor`` domain, a pre-commit
hook, or a test, without going through argv.
"""

from __future__ import annotations

from pathlib import Path

from ..constants import APM_YML_FILENAME
from .checks import check_lockfile, check_manifest, check_paths, check_structure
from .model import CHECK_FAMILIES, Finding, Severity, ValidationReport


def run_validation(
    project_root: Path,
    *,
    manifest_path: Path | None = None,
    checks: tuple[str, ...] | None = None,
) -> ValidationReport:
    """Validate ``project_root`` offline and return a structured report.

    Args:
        project_root: Directory to validate.
        manifest_path: Manifest to read. Defaults to ``project_root/apm.yml``.
        checks: Families to run, from :data:`CHECK_FAMILIES`. ``None`` runs all.

    Returns:
        A report. Never raises for project-level problems -- those are
        findings. Only a caller error (an unknown check family) raises.
    """
    selected = _resolve_checks(checks)
    manifest_path = manifest_path or project_root / APM_YML_FILENAME

    findings: list[Finding] = []
    document, manifest_findings, package = check_manifest(manifest_path)

    # The manifest family always runs: every other family needs a parsed
    # document, and a caller who deselected it still needs to know why the
    # others were skipped. Its findings are only *reported* when selected.
    if "manifest" in selected:
        findings.extend(manifest_findings)

    if document is not None:
        manifest_rejected = any(f.code == "manifest.rejected" for f in manifest_findings)
        if "structure" in selected:
            findings.extend(check_structure(project_root, manifest_rejected=manifest_rejected))
        if "paths" in selected:
            findings.extend(check_paths(project_root, document, package))
        if "lockfile" in selected:
            findings.extend(check_lockfile(project_root, package))
    elif "manifest" not in selected:
        # Unparseable manifest, and the caller deselected the family that
        # explains it. Say so rather than reporting a silent pass.
        findings.append(
            Finding(
                code="manifest.not-loaded",
                severity=Severity.ERROR,
                check="manifest",
                message=(
                    f"{APM_YML_FILENAME} could not be loaded, so the selected checks "
                    "could not run. Re-run with '--check manifest' for the reason."
                ),
            )
        )

    return ValidationReport(
        root=project_root,
        manifest_path=manifest_path,
        findings=tuple(_ordered(findings)),
        checks_run=selected,
        name=_identity(document, "name"),
        version=_identity(document, "version"),
    )


def _resolve_checks(checks: tuple[str, ...] | None) -> tuple[str, ...]:
    """Normalize a requested family selection to report order."""
    if not checks:
        return CHECK_FAMILIES
    unknown = sorted(set(checks) - set(CHECK_FAMILIES))
    if unknown:
        raise ValueError(f"Unknown check family/ies: {', '.join(unknown)}")
    return tuple(family for family in CHECK_FAMILIES if family in set(checks))


def _ordered(findings: list[Finding]) -> list[Finding]:
    """Errors first, then by family in report order, stable within a group."""
    family_rank = {family: index for index, family in enumerate(CHECK_FAMILIES)}
    return sorted(
        findings,
        key=lambda f: (
            0 if f.severity is Severity.ERROR else 1,
            family_rank.get(f.check, len(family_rank)),
        ),
    )


def _identity(document: dict | None, key: str) -> str | None:
    """Read one identity field for the report header, defensively."""
    if not document:
        return None
    value = document.get(key)
    return str(value) if isinstance(value, (str, int, float)) else None

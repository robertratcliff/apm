"""Offline checks behind ``apm validate``.

Every function here is read-only and network-free: nothing in this module
writes to disk, creates user config, resolves a dependency, or contacts a
registry. That is what makes ``apm validate`` safe to run in CI, in a
pre-commit hook, or against a directory the caller does not own.

The checks deliberately delegate verdicts to the code that already owns them
rather than re-implementing the schema:

* manifest semantics  -> :meth:`APMPackage.from_mapping` (the loader is the
  authority on what APM can actually consume)
* project shape       -> :func:`validate_apm_package` (the install-time gate)
* ``includes`` safety -> the same three rules ``apm pack`` enforces
* lockfile identity   -> ``get_canonical_dependency_string`` on both sides

See :mod:`apm_cli.validation.model` for the severity contract.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any

import yaml

from ..constants import APM_YML_FILENAME
from ..core.errors import TargetResolutionError
from .model import Finding, Severity

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from ..models.apm_package import APMPackage

# Spec 3.2: a version SHOULD be semver; a deviation is a warning, not a failure.
_SEMVER_PREFIX = re.compile(r"^\d+\.\d+\.\d+")

# Top-level keys the manifest specification defines (reference/manifest-schema,
# section 2), plus ``lifecycle``, which the CLI reads from apm.yml
# (core/lifecycle_scripts.py) though the schema page does not yet list it.
# Unknown keys are only ever a warning: the spec says writers MUST preserve
# them and resolvers MAY ignore them. The warning exists to catch typos
# (``depenencies:``) that would otherwise install nothing, silently.
KNOWN_TOP_LEVEL_KEYS = frozenset(
    {
        "$schema",
        "name",
        "version",
        "description",
        "author",
        "license",
        "target",
        "targets",
        "type",
        "scripts",
        "includes",
        "registries",
        "dependencies",
        "devDependencies",
        "compilation",
        "policy",
        "marketplace",
        "lifecycle",
    }
)

# Absent metadata is worth a nudge: each of these is inherited by the
# marketplace block or recorded in a consumer's lockfile when present.
_RECOMMENDED_FIELDS = ("description", "author", "license")

# Markers that make a directory resolvable as a local dependency. Mirrors the
# package-type detection in models/validation.py.
_PACKAGE_MARKERS = (APM_YML_FILENAME, "SKILL.md", "plugin.json")
_PACKAGE_MARKER_DIRS = (".claude-plugin", "skills", ".apm")

# read_only=True makes validate_apm_package refuse formats it would otherwise
# normalize. That refusal describes the read-only mode, not a project defect,
# so it is dropped rather than reported.
_READ_ONLY_ARTEFACT = "Plugin requires normalization"


def check_manifest(manifest_path: Path) -> tuple[dict | None, list[Finding], APMPackage | None]:
    """Load and check ``apm.yml``.

    Returns:
        ``(document, findings, package)``. ``document`` is ``None`` when the
        file could not be parsed into a mapping at all, in which case no
        further family can run. ``package`` is ``None`` when the document
        parsed but APM's own loader rejected its contents.
    """
    from ..utils.yaml_io import load_yaml

    if not manifest_path.exists():
        return (
            None,
            [
                Finding(
                    code="manifest.not-found",
                    severity=Severity.ERROR,
                    check="manifest",
                    message=f"No {APM_YML_FILENAME} found in {manifest_path.parent}.",
                    hint="Run 'apm init' to scaffold one, or pass the project directory.",
                )
            ],
            None,
        )

    try:
        document = load_yaml(manifest_path)
    except yaml.YAMLError as exc:
        return (
            None,
            [
                Finding(
                    code="manifest.invalid-yaml",
                    severity=Severity.ERROR,
                    check="manifest",
                    message=f"{APM_YML_FILENAME} is not parseable YAML: {exc}",
                    hint="Fix the YAML syntax; every other check needs a parseable manifest.",
                )
            ],
            None,
        )
    except OSError as exc:
        return (
            None,
            [
                Finding(
                    code="manifest.unreadable",
                    severity=Severity.ERROR,
                    check="manifest",
                    message=f"Could not read {manifest_path}: {exc}",
                )
            ],
            None,
        )

    if document is None:
        return (
            None,
            [
                Finding(
                    code="manifest.empty",
                    severity=Severity.ERROR,
                    check="manifest",
                    message=f"{APM_YML_FILENAME} is empty.",
                    hint="A manifest MUST declare at least 'name' and 'version'.",
                )
            ],
            None,
        )

    if not isinstance(document, dict):
        return (
            None,
            [
                Finding(
                    code="manifest.not-mapping",
                    severity=Severity.ERROR,
                    check="manifest",
                    message=(
                        f"{APM_YML_FILENAME} must be a YAML mapping at the top level, "
                        f"got {type(document).__name__}."
                    ),
                )
            ],
            None,
        )

    findings: list[Finding] = []
    package = _load_package(manifest_path, document, findings)
    findings.extend(_spec_advisories(document))
    return document, findings, package


def _load_package(
    manifest_path: Path,
    document: dict,
    findings: list[Finding],
) -> APMPackage | None:
    """Run the real loader; surface its rejection verbatim as one error."""
    from ..models.apm_package import APMPackage

    try:
        return APMPackage.from_mapping(
            document,
            package_path=manifest_path.parent,
            manifest_path=manifest_path,
            create_config=False,
        )
    except (
        TargetResolutionError,
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
        OSError,
    ) as exc:
        # ValueError covers most of the loader's own diagnostics (missing
        # name/version, bad dependency reference, unsupported $schema).
        # TargetResolutionError is separate because that family subclasses
        # click.UsageError, not ValueError -- uncaught, a bad 'targets:' block
        # would exit 2 through Click and bypass the report entirely. The rest
        # are defensive: a hostile manifest must produce a finding, never a
        # traceback.
        findings.append(
            Finding(
                code="manifest.rejected",
                severity=Severity.ERROR,
                check="manifest",
                message=f"APM cannot load this manifest: {exc}",
                hint="Until this is fixed, install, compile and pack fail the same way.",
            )
        )
        return None


def _spec_advisories(document: dict) -> list[Finding]:
    """Spec deviations the loader tolerates. All warnings, by construction."""
    findings: list[Finding] = []

    version = document.get("version")
    if version is not None and not _SEMVER_PREFIX.match(str(version).strip()):
        findings.append(
            Finding(
                code="manifest.version-not-semver",
                severity=Severity.WARNING,
                check="manifest",
                field="version",
                message=f"Version '{version}' does not follow semantic versioning (x.y.z).",
                hint="Consumers range-match on semver; a non-semver version cannot be pinned.",
            )
        )

    for key in sorted((k for k in document if k not in KNOWN_TOP_LEVEL_KEYS), key=str):
        findings.append(
            Finding(
                code="manifest.unknown-key",
                severity=Severity.WARNING,
                check="manifest",
                field=str(key),
                message=f"Unknown top-level key '{key}'; resolvers may ignore it.",
                hint="Check the spelling against the manifest schema, or remove the key.",
            )
        )

    for field in _RECOMMENDED_FIELDS:
        if field not in document:
            findings.append(
                Finding(
                    code="manifest.field-missing",
                    severity=Severity.WARNING,
                    check="manifest",
                    field=field,
                    message=f"No '{field}' declared.",
                    hint=f"Add '{field}:' so consumers and SBOM exports can record it.",
                )
            )
        elif not isinstance(document[field], str):
            findings.append(
                Finding(
                    code="manifest.field-type",
                    severity=Severity.WARNING,
                    check="manifest",
                    field=field,
                    message=(
                        f"'{field}' should be a string, got {type(document[field]).__name__}."
                    ),
                )
            )

    findings.extend(_script_advisories(document.get("scripts")))

    # Note: an empty 'targets:' list is deliberately NOT checked here. The
    # loader fails closed on it (EmptyTargetsListError), so it always surfaces
    # as manifest.rejected; a warning would be unreachable.

    return findings


def _script_advisories(scripts: Any) -> list[Finding]:
    """Check the ``scripts:`` block shape (spec 3.8: map of string to string)."""
    if scripts is None:
        return []
    if not isinstance(scripts, dict):
        return [
            Finding(
                code="manifest.scripts-not-mapping",
                severity=Severity.WARNING,
                check="manifest",
                field="scripts",
                message=f"'scripts' should be a mapping, got {type(scripts).__name__}.",
                hint="'apm run' cannot resolve a script from this shape.",
            )
        ]
    return [
        Finding(
            code="manifest.script-not-string",
            severity=Severity.WARNING,
            check="manifest",
            field=f"scripts.{name}",
            message=(f"Script '{name}' should be a string command, got {type(body).__name__}."),
        )
        for name, body in scripts.items()
        if not isinstance(body, str)
    ]


def check_structure(project_root: Path, *, manifest_rejected: bool) -> list[Finding]:
    """Report project shape via the install-time admission gate.

    Every finding here is a WARNING on purpose. ``validate_apm_package`` judges
    whether a directory is installable *as a dependency*, and by that standard a
    perfectly healthy consumer project is "invalid" -- it ships no primitives of
    its own. Promoting those to errors would fail a freshly scaffolded project,
    breaking the invariant in :mod:`apm_cli.validation.model`. ``--strict``
    gives producers the gate they want without imposing it on consumers.
    """
    from ..models.validation import validate_apm_package

    try:
        result = validate_apm_package(project_root, read_only=True)
    except (TargetResolutionError, ValueError, TypeError, OSError) as exc:
        # validate_apm_package reloads the manifest and only swallows
        # ValueError/FileNotFoundError itself, so a TargetResolutionError from
        # a bad 'targets:' block reaches us here.
        if manifest_rejected:
            # Same root cause, already reported verbatim by the manifest family.
            return []
        return [
            Finding(
                code="structure.check-failed",
                severity=Severity.WARNING,
                check="structure",
                message=f"Could not inspect project structure: {exc}",
            )
        ]

    findings: list[Finding] = []
    for message in result.errors:
        if _READ_ONLY_ARTEFACT in message:
            continue
        # The manifest family already printed the loader's rejection verbatim.
        if manifest_rejected and message.startswith("Invalid apm.yml:"):
            continue
        findings.append(
            Finding(
                code="structure.advisory",
                severity=Severity.WARNING,
                check="structure",
                message=message,
            )
        )
    findings.extend(
        Finding(
            code="structure.advisory",
            severity=Severity.WARNING,
            check="structure",
            message=message,
        )
        for message in result.warnings
    )
    return findings


def check_paths(project_root: Path, document: dict, package: APMPackage | None) -> list[Finding]:
    """Check that paths the manifest declares actually exist on disk."""
    findings = _check_local_dependencies(project_root, package)
    findings.extend(_check_includes(project_root, document.get("includes")))
    return findings


def _check_local_dependencies(project_root: Path, package: APMPackage | None) -> list[Finding]:
    """Resolve every local dependency path through the real anchoring rules."""
    if package is None:
        return []
    # Private helper, but it is the single owner of local-path anchoring and is
    # already imported this way by adopt/manifest_edit.py.
    from ..deps.path_anchoring import _join

    findings: list[Finding] = []
    for ref in package.get_all_apm_dependencies():
        if not ref.is_local or not ref.local_path:
            continue
        field = f"dependencies.apm[{ref.local_path}]"
        try:
            target = _join(project_root, ref.local_path)
        except (ValueError, OSError) as exc:
            findings.append(
                Finding(
                    code="paths.local-dependency-unresolvable",
                    severity=Severity.ERROR,
                    check="paths",
                    field=field,
                    message=f"Local dependency '{ref.local_path}' cannot be resolved: {exc}",
                )
            )
            continue

        if not target.exists():
            findings.append(
                Finding(
                    code="paths.local-dependency-missing",
                    severity=Severity.ERROR,
                    check="paths",
                    field=field,
                    message=f"Local dependency '{ref.local_path}' does not exist ({target}).",
                    hint="Create the package there, or correct the path in apm.yml.",
                )
            )
        elif not target.is_dir():
            findings.append(
                Finding(
                    code="paths.local-dependency-missing",
                    severity=Severity.ERROR,
                    check="paths",
                    field=field,
                    message=f"Local dependency '{ref.local_path}' is not a directory ({target}).",
                )
            )
        elif not _looks_like_package(target):
            findings.append(
                Finding(
                    code="paths.local-dependency-not-a-package",
                    severity=Severity.WARNING,
                    check="paths",
                    field=field,
                    message=(
                        f"Local dependency '{ref.local_path}' exists but carries no "
                        f"package marker ({', '.join(_PACKAGE_MARKERS)})."
                    ),
                    hint="Install will refuse it as a package; check the path points at the root.",
                )
            )
    return findings


def _looks_like_package(path: Path) -> bool:
    """Whether a directory carries any marker that makes it installable."""
    if any((path / marker).is_file() for marker in _PACKAGE_MARKERS):
        return True
    return any((path / marker).is_dir() for marker in _PACKAGE_MARKER_DIRS)


def _safe_relative_parts(raw: str) -> tuple[str, ...]:
    """Split an ``includes`` entry into safe relative parts.

    Mirrors ``_deployed_path_parts`` in bundle/plugin_exporter.py so validate
    rejects exactly what pack rejects.
    """
    normalized = raw.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or PureWindowsPath(normalized).is_absolute():
        raise ValueError("path is absolute")
    parts = pure.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ValueError("path contains unsafe segments")
    return parts


def _check_includes(project_root: Path, includes: Any) -> list[Finding]:
    """Check explicit ``includes:`` entries against pack's three rules.

    Per spec 3.9, form 3 (``includes: [<path>, ...]``) is an exhaustive
    allow-list, and missing, unsafe or symlinked paths stop ``apm pack`` --
    which is why these are errors rather than advisories. ``auto`` and an
    omitted field enumerate nothing and so have nothing to check.
    """
    if not isinstance(includes, list):
        return []

    from ..utils.path_security import PathTraversalError, ensure_path_within

    findings: list[Finding] = []
    for index, raw in enumerate(includes):
        field = f"includes[{index}]"
        if not isinstance(raw, str):
            findings.append(
                Finding(
                    code="paths.includes-not-string",
                    severity=Severity.WARNING,
                    check="paths",
                    field=field,
                    message=f"'includes' entry should be a path string, got {type(raw).__name__}.",
                )
            )
            continue

        try:
            parts = _safe_relative_parts(raw)
        except ValueError as exc:
            findings.append(
                Finding(
                    code="paths.includes-unsafe",
                    severity=Severity.ERROR,
                    check="paths",
                    field=field,
                    message=f"'includes' path {raw!r} is unsafe: {exc}.",
                    hint="Use a relative path under the project root; 'apm pack' refuses this.",
                )
            )
            continue

        candidate = project_root.joinpath(*parts)
        if candidate.is_symlink():
            findings.append(
                Finding(
                    code="paths.includes-symlink",
                    severity=Severity.ERROR,
                    check="paths",
                    field=field,
                    message=f"'includes' path {raw!r} is a symlink.",
                    hint="Replace it with a regular file or directory; 'apm pack' refuses it.",
                )
            )
            continue

        try:
            ensure_path_within(candidate, project_root)
        except PathTraversalError:
            findings.append(
                Finding(
                    code="paths.includes-unsafe",
                    severity=Severity.ERROR,
                    check="paths",
                    field=field,
                    message=f"'includes' path {raw!r} escapes the project root.",
                    hint="Fix the path in apm.yml; 'apm pack' refuses it.",
                )
            )
            continue

        if not candidate.exists():
            findings.append(
                Finding(
                    code="paths.includes-missing",
                    severity=Severity.ERROR,
                    check="paths",
                    field=field,
                    message=f"'includes' path {raw!r} does not exist ({candidate}).",
                    hint="Create it or remove the entry; 'apm pack' stops on a missing include.",
                )
            )
    return findings


def check_lockfile(project_root: Path, package: APMPackage | None) -> list[Finding]:
    """Compare declared APM dependencies against the lockfile.

    Drift is reported at WARNING: the fix is ``apm lock``, not a manifest edit,
    and a project mid-change is not broken. ``--strict`` turns it into a gate.
    """
    if package is None:
        return []

    from ..deps.lockfile import (
        LockFile,
        LockfileFormatError,
        resolve_lockfile_path_for_read,
    )

    declared = {
        ref.get_canonical_dependency_string(): ref for ref in package.get_all_apm_dependencies()
    }
    # read_only=True so resolving the path never migrates a legacy apm.lock.
    lock_path = resolve_lockfile_path_for_read(project_root, read_only=True)

    if not lock_path.exists():
        if not declared:
            return []
        return [
            Finding(
                code="lockfile.not-found",
                severity=Severity.WARNING,
                check="lockfile",
                message=(
                    f"{len(declared)} APM "
                    f"{'dependency' if len(declared) == 1 else 'dependencies'} "
                    f"declared, but no lockfile ({lock_path.name}) exists."
                ),
                hint="Run 'apm lock' so installs are reproducible.",
            )
        ]

    try:
        lock = LockFile.read(lock_path)
    except (LockfileFormatError, ValueError, OSError, yaml.YAMLError) as exc:
        return [
            Finding(
                code="lockfile.unreadable",
                severity=Severity.ERROR,
                check="lockfile",
                message=f"{lock_path.name} cannot be read: {exc}",
                hint="Regenerate it with 'apm lock'.",
            )
        ]

    if lock is None:
        return [
            Finding(
                code="lockfile.unreadable",
                severity=Severity.ERROR,
                check="lockfile",
                message=f"{lock_path.name} could not be parsed into a lockfile.",
                hint="Regenerate it with 'apm lock'.",
            )
        ]

    # Only direct entries are comparable: the lockfile also carries the
    # transitive closure, which the root manifest never declares.
    locked = {
        dep.get_canonical_dependency_string(): dep
        for dep in lock.get_package_dependencies()
        if dep.depth == 1
    }

    findings = [
        Finding(
            code="lockfile.declared-not-locked",
            severity=Severity.WARNING,
            check="lockfile",
            field=key,
            message=f"'{key}' is declared in apm.yml but absent from {lock_path.name}.",
            hint="Run 'apm lock' to resolve it.",
        )
        for key in sorted(set(declared) - set(locked))
    ]
    findings.extend(
        Finding(
            code="lockfile.locked-not-declared",
            severity=Severity.WARNING,
            check="lockfile",
            field=key,
            message=(f"'{key}' is locked as a direct dependency but not declared in apm.yml."),
            hint="Run 'apm lock' to drop it, or re-declare it in apm.yml.",
        )
        for key in sorted(set(locked) - set(declared))
    )
    return findings

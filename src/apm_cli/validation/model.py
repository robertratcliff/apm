"""Structured findings produced by ``apm validate``.

Severity is a public contract -- it decides the process exit code -- so the
split between the two levels is deliberate and narrow:

``ERROR``
    APM itself cannot load this manifest, or a filesystem path the manifest
    declares definitively does not exist. A project in this state is already
    broken for ``apm install`` / ``apm pack``; ``apm validate`` is only
    reporting it earlier.

``WARNING``
    A deviation from the manifest specification, or an advisory about project
    shape, that the current loader tolerates. Reported always, but it fails
    the command only under ``--strict``.

That split exists to protect one invariant:

    ``apm validate`` MUST NOT fail a project that ``apm install`` accepts.

New checks must preserve it. A check that would fail a currently-installable
project belongs at ``WARNING``, where ``--strict`` users can still gate on it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

# Check families, in report order. Each is independently selectable via
# ``apm validate --check <family>``, so these names are public.
CHECK_FAMILIES: tuple[str, ...] = ("manifest", "structure", "paths", "lockfile")


class Severity(str, Enum):
    """Finding severity. ``str`` mixin keeps JSON serialization trivial."""

    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class Finding:
    """One validation result.

    Attributes:
        code: Stable machine-readable identifier (``family.kebab-case``).
            Consumers may match on it, so a code's meaning must not change
            once released; retire and replace instead.
        severity: See the module docstring.
        check: Family this finding came from (one of :data:`CHECK_FAMILIES`).
        message: Complete human-readable sentence describing the problem.
        field: Manifest field or path the finding points at, when it has one.
        hint: Actionable next step, when there is an obvious one.
    """

    code: str
    severity: Severity
    check: str
    message: str
    field: str | None = None
    hint: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        """Serialize for ``--format json``."""
        return {
            "code": self.code,
            "severity": self.severity.value,
            "check": self.check,
            "field": self.field,
            "message": self.message,
            "hint": self.hint,
        }


@dataclass(frozen=True)
class ValidationReport:
    """Aggregate outcome of a validation run."""

    root: Path
    manifest_path: Path
    findings: tuple[Finding, ...]
    checks_run: tuple[str, ...]
    name: str | None = None
    version: str | None = None

    @property
    def errors(self) -> tuple[Finding, ...]:
        """Findings at ERROR severity."""
        return tuple(f for f in self.findings if f.severity is Severity.ERROR)

    @property
    def warnings(self) -> tuple[Finding, ...]:
        """Findings at WARNING severity."""
        return tuple(f for f in self.findings if f.severity is Severity.WARNING)

    def ok(self, *, strict: bool = False) -> bool:
        """Whether the project passes, honouring ``--strict``."""
        if self.errors:
            return False
        return not (strict and self.warnings)

    def exit_code(self, *, strict: bool = False) -> int:
        """Process exit code: 0 when :meth:`ok`, else 1."""
        return 0 if self.ok(strict=strict) else 1

    def to_dict(self, *, strict: bool = False) -> dict:
        """Serialize the whole report for ``--format json``."""
        return {
            "root": str(self.root),
            "manifest": str(self.manifest_path),
            "name": self.name,
            "version": self.version,
            "checks": list(self.checks_run),
            "strict": strict,
            "ok": self.ok(strict=strict),
            "summary": {
                "errors": len(self.errors),
                "warnings": len(self.warnings),
            },
            "findings": [f.to_dict() for f in self.findings],
        }

    def to_json(self, *, strict: bool = False) -> str:
        """Pretty-printed JSON form of :meth:`to_dict`."""
        return json.dumps(self.to_dict(strict=strict), indent=2)

"""``apm doctor`` command implementation.

The diagnostics are expressed as a registry of small, independent check
functions (:data:`_CHECK_REGISTRY`).  :func:`collect_doctor_checks` runs
them and returns plain data; :func:`run_doctor` renders that data and
derives the exit code.  Keeping collection and rendering apart lets each
check be unit-tested directly, without a CliRunner, and leaves room for a
second renderer that consumes the same results.

This commit is a pure restructure: every check keeps the behaviour, wording
and exit-code contribution it had before.

Every check runs behind :func:`_guarded`, so an unexpected exception in
one check degrades to a single failed row instead of aborting the whole
report -- the failure mode that matters most for a tool people reach for
precisely when their environment is broken.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...core.command_logger import CommandLogger
from ...marketplace.errors import MarketplaceYmlError
from ...marketplace.git_stderr import translate_git_stderr
from ...marketplace.migration import ConfigSource, detect_config_source
from ...marketplace.output_profiles import known_output_names
from ...marketplace.yml_schema import (
    load_marketplace_from_apm_yml,
    load_marketplace_yml,
)
from . import (
    _DoctorCheck,
    _find_duplicate_names,
    _render_doctor_table,
)

#: ``git --version`` is a local exec; 5s is generous.
GIT_VERSION_TIMEOUT_SECONDS = 5

#: Budget for the outbound ``git ls-remote`` probe.
NETWORK_PROBE_TIMEOUT_SECONDS = 5

#: Public repository used to prove outbound git-over-HTTPS works.
NETWORK_PROBE_URL = "https://github.com/git/git.git"


@dataclass
class _DoctorRun:
    """Mutable state shared by the checks of a single doctor invocation."""

    project_root: Path
    logger: CommandLogger
    #: Set by the marketplace-config check; later checks key off it.
    yml: Any = None
    notes: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        """Record a diagnostic line about how a check ran."""
        self.notes.append(message)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_git(run: _DoctorRun) -> _DoctorCheck:
    """Check 1: git is on PATH and executable."""
    git_ok = False
    git_detail = ""
    try:
        result = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            text=True,
            timeout=GIT_VERSION_TIMEOUT_SECONDS,
        )
        if result.returncode == 0:
            git_ok = True
            git_detail = result.stdout.strip()
        else:
            git_detail = "git returned non-zero exit code"
    except FileNotFoundError:
        git_detail = "git not found on PATH"
    except subprocess.TimeoutExpired:
        git_detail = "git --version timed out"
    except (subprocess.SubprocessError, OSError) as exc:
        git_detail = str(exc)[:60]
    return _DoctorCheck(name="git", passed=git_ok, detail=git_detail)


def _check_network(run: _DoctorRun) -> _DoctorCheck:
    """Check 2: outbound git-over-HTTPS reaches github.com."""
    timeout = NETWORK_PROBE_TIMEOUT_SECONDS
    run.note(f"network probe: git ls-remote {NETWORK_PROBE_URL} (timeout {timeout}s)")
    net_ok = False
    net_detail = ""
    try:
        result = subprocess.run(
            ["git", "ls-remote", NETWORK_PROBE_URL, "HEAD"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            net_ok = True
            net_detail = "github.com reachable"
        else:
            translated = translate_git_stderr(
                result.stderr,
                exit_code=result.returncode,
                operation="ls-remote",
                remote="github.com",
            )
            net_detail = translated.hint[:80]
    except subprocess.TimeoutExpired:
        net_detail = f"Network check timed out ({timeout}s)"
    except FileNotFoundError:
        net_detail = "git not found; cannot test network"
    except (subprocess.SubprocessError, OSError) as exc:
        net_detail = str(exc)[:60]
    return _DoctorCheck(name="network", passed=net_ok, detail=net_detail)


def _check_auth(run: _DoctorRun) -> _DoctorCheck:
    """Check 3: a credential is resolvable for github.com (informational)."""
    try:
        from ...core.auth import AuthResolver

        resolver = AuthResolver()
        # Try to get a token for github.com as a representative check
        token = resolver.resolve("github.com").token
        has_token = bool(token)
    except Exception as exc:
        run.note(f"auth probe raised {type(exc).__name__}: {exc}")
        has_token = False
    detail = "Token detected" if has_token else "No token; unauthenticated rate limits apply"
    return _DoctorCheck(name="auth", passed=True, detail=detail, informational=True)


def _check_marketplace_config(run: _DoctorRun) -> _DoctorCheck:
    """Check 4: marketplace authoring config is present and parsable.

    Stores the parsed config on *run* so the coverage, duplicate-name and
    version-alignment checks can reuse it.
    """
    apm_path = run.project_root / "apm.yml"
    legacy_path = run.project_root / "marketplace.yml"
    config_passed = True
    config_detail = ""

    try:
        source = detect_config_source(run.project_root)
        if source == ConfigSource.APM_YML:
            try:
                run.yml = load_marketplace_from_apm_yml(apm_path)
                config_detail = "apm.yml 'marketplace:' block found and valid"
            except MarketplaceYmlError as exc:
                config_passed = False
                config_detail = f"apm.yml marketplace block has errors: {str(exc)[:60]}"
        elif source == ConfigSource.LEGACY_YML:
            try:
                run.yml = load_marketplace_yml(legacy_path)
                config_detail = (
                    "marketplace.yml found (legacy). Run 'apm marketplace "
                    "migrate' to fold it into apm.yml."
                )
            except MarketplaceYmlError as exc:
                config_passed = False
                config_detail = f"marketplace.yml has errors: {str(exc)[:60]}"
        else:
            config_detail = "No marketplace authoring config in current directory"
    except MarketplaceYmlError as exc:
        config_passed = False
        config_detail = str(exc)[:120]

    return _DoctorCheck(
        name="marketplace config",
        passed=config_passed,
        detail=config_detail,
        informational=True,
    )


def _check_format_coverage(run: _DoctorRun) -> _DoctorCheck | None:
    """Check 5: which consumer formats the marketplace publishes for."""
    if run.yml is None:
        return None
    configured = frozenset(getattr(run.yml, "outputs", ()) or ())
    supported = known_output_names()
    missing = sorted(supported - configured)
    configured_sorted = sorted(configured)
    if not missing:
        detail = f"Publishing for all known formats: {', '.join(configured_sorted)}."
    else:
        detail = (
            f"Configured: {', '.join(configured_sorted) or '(none)'}. "
            f"Also supported: {', '.join(missing)}. "
            f"Add e.g. '{missing[0]}: {{}}' under 'marketplace.outputs' "
            "in apm.yml to publish for more consumers."
        )
    return _DoctorCheck(
        name="format coverage",
        passed=True,  # informational; never fails
        detail=detail,
        informational=True,
    )


def _check_duplicate_names(run: _DoctorRun) -> _DoctorCheck | None:
    """Check 6: duplicate package names (defence-in-depth behind the schema)."""
    if run.yml is None:
        return None
    dup_detail = _find_duplicate_names(run.yml)
    return _DoctorCheck(
        name="duplicate names",
        passed=not dup_detail,
        detail=dup_detail if dup_detail else "No duplicate package names",
        informational=True,
    )


def _check_version_alignment(run: _DoctorRun) -> _DoctorCheck | None:
    """Check 7: local package versions match the configured strategy."""
    if run.yml is None or not hasattr(run.yml, "versioning"):
        return None
    from ...marketplace.version_check import check_version_alignment

    report = check_version_alignment(run.yml, run.project_root)
    total = len(report.packages)
    aligned = sum(1 for p in report.packages if p.ok)
    if total == 0:
        detail = f"strategy={report.strategy}, no local packages to align"
        passed = True
    elif report.ok:
        detail = f"strategy={report.strategy}, {aligned}/{total} packages aligned"
        passed = True
    else:
        misaligned = [p.path for p in report.packages if not p.ok]
        detail = (
            f"strategy={report.strategy}, "
            f"{len(misaligned)}/{total} packages misaligned: "
            f"{misaligned[0]}"
        )
        passed = False
    return _DoctorCheck(
        name="version alignment",
        passed=passed,
        detail=detail,
        informational=True,
    )


def _executable_trust_drift_check(
    project_root: Path, logger: CommandLogger | None = None
) -> _DoctorCheck | None:
    """Fleet-level executable-trust drift probe for ``apm doctor``.

    Flags packages whose project/user *allow* is overridden by the org
    *deny* ceiling -- a governance conflict an admin should reconcile. Best
    effort and informational: any failure to resolve degrades to ``None`` so
    doctor never hangs or hard-fails on policy discovery. Points the operator
    at ``apm policy explain <pkg>`` for the per-package detail.
    """
    apm_path = project_root / "apm.yml"
    if not apm_path.is_file():
        return None
    try:
        from ...security.executables import (
            LAYER_ORG_DENY,
            LAYER_ORG_DENY_ALL,
            LAYER_PROJECT_ALLOW,
            LAYER_USER_ALLOW,
            build_exec_trust_context,
            resolve_exec_decision,
        )
        from ...utils.yaml_io import load_yaml
        from ..approve import load_org_policy, scan_installed_executable_packages

        data = load_yaml(apm_path)
        project_data = data if isinstance(data, dict) else {}
        ctx = build_exec_trust_context(
            policy=load_org_policy(project_root, logger=logger),
            project_data=project_data,
        )
    except Exception:
        return None

    if not ctx.gate_enabled:
        return _DoctorCheck(
            name="executable trust",
            passed=True,
            detail="Gate disabled (no executables: block in apm.yml)",
            informational=True,
        )

    org_deny_layers = (LAYER_ORG_DENY_ALL, LAYER_ORG_DENY)
    allow_layers = (LAYER_PROJECT_ALLOW, LAYER_USER_ALLOW)
    conflicts: list[str] = []
    try:
        for decl in scan_installed_executable_packages(apm_path):
            if not getattr(decl, "has_executables", False):
                continue
            for exec_type in decl.exec_types:
                decision = resolve_exec_decision(ctx, decl.package_key, exec_type)
                if decision.deciding_layer in org_deny_layers and any(
                    layer in decision.shadowed_layers for layer in allow_layers
                ):
                    conflicts.append(decl.package_name)
                    break
    except Exception:
        return None

    if not conflicts:
        return _DoctorCheck(
            name="executable trust",
            passed=True,
            detail="No executable-trust layer conflicts",
            informational=True,
        )
    first = conflicts[0]
    return _DoctorCheck(
        name="executable trust",
        passed=False,
        detail=(
            f"{len(conflicts)} package(s) allowed locally but denied by org policy "
            f"(e.g. {first}). Run 'apm policy explain {first}' for detail."
        ),
        informational=True,
    )


def _check_executable_trust(run: _DoctorRun) -> _DoctorCheck | None:
    """Check 8: executable-trust drift, rooted at the discovered project."""
    return _executable_trust_drift_check(run.project_root, logger=run.logger)


# ---------------------------------------------------------------------------
# Registry, collection, reporting
# ---------------------------------------------------------------------------

#: ``(name, critical, fn)``.  ``critical`` checks are the only ones that can
#: drive a non-zero exit code; everything else is advisory.  Tuple order is
#: render order.  A new diagnostic is one appended row plus one function.
_CHECK_REGISTRY: tuple[tuple[str, bool, Callable[[_DoctorRun], _DoctorCheck | None]], ...] = (
    ("git", True, _check_git),
    ("network", True, _check_network),
    ("auth", False, _check_auth),
    ("marketplace config", False, _check_marketplace_config),
    ("format coverage", False, _check_format_coverage),
    ("duplicate names", False, _check_duplicate_names),
    ("version alignment", False, _check_version_alignment),
    ("executable trust", False, _check_executable_trust),
)


def _guarded(
    name: str,
    critical: bool,
    fn: Callable[[_DoctorRun], _DoctorCheck | None],
    run: _DoctorRun,
) -> _DoctorCheck | None:
    """Run *fn*, converting an unexpected exception into a reported row.

    A diagnostic tool that dies with a traceback on the very environment it
    was invoked to explain is worse than useless, so no single check is
    allowed to abort the report.
    """
    try:
        return fn(run)
    except Exception as exc:
        run.note(f"{name} check raised {type(exc).__name__}: {exc}")
        return _DoctorCheck(
            name=name,
            passed=False,
            detail=f"Check failed: {type(exc).__name__}: {exc}"[:120],
            informational=not critical,
        )


def collect_doctor_checks(run: _DoctorRun) -> list[_DoctorCheck]:
    """Run every registered check and return the results in render order."""
    checks: list[_DoctorCheck] = []
    for name, critical, fn in _CHECK_REGISTRY:
        check = _guarded(name, critical, fn, run)
        if check is not None:
            checks.append(check)
        else:
            run.note(f"{name}: not applicable, row omitted")
    return checks


def run_doctor(verbose: bool, *, logger_name: str = "doctor") -> int:
    """Execute the doctor diagnostics and return an exit code.

    Called by the top-level ``apm doctor`` command.
    Returns ``0`` if all critical checks pass, ``1`` otherwise.
    """
    logger = CommandLogger(logger_name, verbose=verbose)
    run = _DoctorRun(project_root=Path.cwd(), logger=logger)

    checks = collect_doctor_checks(run)

    _render_doctor_table(logger, checks)

    # Exit: 0 if the critical checks pass; config checks are informational.
    critical_failed = any(not c.passed for c in checks if not c.informational)
    return 1 if critical_failed else 0

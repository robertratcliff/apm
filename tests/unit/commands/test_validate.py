"""Tests for ``apm validate`` and the validation engine behind it.

The load-bearing contract here is the severity split documented in
``apm_cli.validation.model``: errors are things APM genuinely cannot load or
resolve, warnings are spec deviations the loader tolerates, and validate must
never fail a project that ``apm install`` would accept. Several tests below
exist specifically to pin that invariant.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.validation import CHECK_FAMILIES, Severity, run_validation

pytestmark = pytest.mark.component

_MINIMAL = """\
name: demo
version: 1.0.0
description: A demo project
author: Test Author
license: MIT
includes: auto
dependencies:
  apm: []
  mcp: []
scripts: {}
"""


def _project(tmp_path: Path, manifest: str = _MINIMAL, *, with_apm_dir: bool = True) -> Path:
    """Write a project directory and return its root."""
    (tmp_path / "apm.yml").write_text(manifest, encoding="utf-8")
    if with_apm_dir:
        instructions = tmp_path / ".apm" / "instructions"
        instructions.mkdir(parents=True)
        (instructions / "main.md").write_text("# Instructions\n\nBe helpful.\n", encoding="utf-8")
    return tmp_path


def _codes(report) -> list[str]:
    return [f.code for f in report.findings]


# --------------------------------------------------------------------------
# Registration and help surface
# --------------------------------------------------------------------------


def test_validate_is_registered_at_top_level():
    result = CliRunner().invoke(cli, ["validate", "--help"])

    assert result.exit_code == 0
    assert "Validate apm.yml" in result.output


def test_validate_appears_in_root_help():
    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "validate" in result.output


def test_validate_help_documents_every_check_family():
    result = CliRunner().invoke(cli, ["validate", "--help"])

    for family in CHECK_FAMILIES:
        assert family in result.output


# --------------------------------------------------------------------------
# The happy path, and the invariant it protects
# --------------------------------------------------------------------------


def test_clean_project_passes_with_no_findings(tmp_path):
    report = run_validation(_project(tmp_path))

    assert report.findings == ()
    assert report.ok() is True
    assert report.exit_code() == 0


def test_clean_project_passes_under_strict(tmp_path):
    report = run_validation(_project(tmp_path))

    assert report.ok(strict=True) is True
    assert report.exit_code(strict=True) == 0


def test_freshly_scaffolded_project_does_not_error(tmp_path):
    """The invariant: no .apm/ and no dependencies is installable, so no error.

    ``apm init`` scaffolds exactly this shape. If structure advisories were
    promoted to errors, ``apm validate`` would fail immediately after
    ``apm init`` -- see apm_cli.validation.model.
    """
    report = run_validation(_project(tmp_path, with_apm_dir=False))

    assert report.errors == ()
    assert report.exit_code() == 0
    assert any(f.check == "structure" for f in report.findings)
    assert all(f.severity is Severity.WARNING for f in report.findings)


def test_command_exits_zero_on_a_clean_project(tmp_path):
    result = CliRunner().invoke(cli, ["validate", str(_project(tmp_path))])

    assert result.exit_code == 0
    assert "no issues found" in result.output


# --------------------------------------------------------------------------
# Manifest family -- errors
# --------------------------------------------------------------------------


def test_missing_manifest_is_an_error(tmp_path):
    report = run_validation(tmp_path)

    assert "manifest.not-found" in _codes(report)
    assert report.exit_code() == 1


def test_unparseable_yaml_is_an_error(tmp_path):
    (tmp_path / "apm.yml").write_text("name: demo\n  version: [unclosed\n", encoding="utf-8")

    report = run_validation(tmp_path)

    assert "manifest.invalid-yaml" in _codes(report)
    assert report.exit_code() == 1


def test_empty_manifest_is_an_error(tmp_path):
    (tmp_path / "apm.yml").write_text("", encoding="utf-8")

    report = run_validation(tmp_path)

    assert "manifest.empty" in _codes(report)


def test_non_mapping_manifest_is_an_error(tmp_path):
    (tmp_path / "apm.yml").write_text("- just\n- a\n- list\n", encoding="utf-8")

    report = run_validation(tmp_path)

    assert "manifest.not-mapping" in _codes(report)


def test_missing_version_is_reported_as_a_loader_rejection(tmp_path):
    report = run_validation(_project(tmp_path, "name: demo\ndescription: d\n"))

    assert "manifest.rejected" in _codes(report)
    rejection = next(f for f in report.findings if f.code == "manifest.rejected")
    assert "version" in rejection.message
    assert report.exit_code() == 1


def test_unknown_target_value_is_a_loader_rejection(tmp_path):
    manifest = _MINIMAL + "targets:\n  - not-a-real-target\n"

    report = run_validation(_project(tmp_path, manifest))

    assert "manifest.rejected" in _codes(report)


def test_unsupported_schema_contract_is_a_loader_rejection(tmp_path):
    manifest = '$schema: "https://example.invalid/made-up.json"\n' + _MINIMAL

    report = run_validation(_project(tmp_path, manifest))

    assert "manifest.rejected" in _codes(report)


def test_a_rejected_manifest_does_not_duplicate_the_structure_diagnostic(tmp_path):
    """The loader's message is printed once, by the manifest family."""
    report = run_validation(_project(tmp_path, "name: demo\n"))

    assert not [f for f in report.findings if f.message.startswith("Invalid apm.yml:")]


# --------------------------------------------------------------------------
# Manifest family -- warnings
# --------------------------------------------------------------------------


def test_non_semver_version_warns_but_passes(tmp_path):
    report = run_validation(_project(tmp_path, _MINIMAL.replace("1.0.0", "v1")))

    assert "manifest.version-not-semver" in _codes(report)
    assert report.errors == ()
    assert report.exit_code() == 0
    assert report.exit_code(strict=True) == 1


def test_unknown_top_level_key_warns(tmp_path):
    """A typo'd dependency block installs nothing today; surface it."""
    report = run_validation(_project(tmp_path, _MINIMAL + "depenencies:\n  apm: []\n"))

    finding = next(f for f in report.findings if f.code == "manifest.unknown-key")
    assert finding.field == "depenencies"
    assert finding.severity is Severity.WARNING
    assert report.exit_code() == 0


def test_known_top_level_keys_do_not_warn(tmp_path):
    """lifecycle: is read from apm.yml even though the schema page omits it."""
    manifest = _MINIMAL + "lifecycle:\n  postinstall: echo hi\n"

    report = run_validation(_project(tmp_path, manifest))

    assert "manifest.unknown-key" not in _codes(report)


def test_absent_recommended_fields_warn(tmp_path):
    report = run_validation(_project(tmp_path, "name: demo\nversion: 1.0.0\n"))

    missing = {f.field for f in report.findings if f.code == "manifest.field-missing"}
    assert missing == {"description", "author", "license"}


def test_wrong_type_on_a_recommended_field_warns(tmp_path):
    report = run_validation(_project(tmp_path, _MINIMAL.replace("license: MIT", "license: 42")))

    assert "manifest.field-type" in _codes(report)
    assert report.errors == ()


def test_scripts_not_a_mapping_warns(tmp_path):
    manifest = _MINIMAL.replace("scripts: {}", "scripts:\n  - build\n")

    report = run_validation(_project(tmp_path, manifest))

    assert "manifest.scripts-not-mapping" in _codes(report)


def test_non_string_script_body_warns(tmp_path):
    manifest = _MINIMAL.replace("scripts: {}", "scripts:\n  build: [not, a, string]\n")

    report = run_validation(_project(tmp_path, manifest))

    finding = next(f for f in report.findings if f.code == "manifest.script-not-string")
    assert finding.field == "scripts.build"


def test_empty_targets_list_is_a_loader_rejection(tmp_path):
    """Regression: this family subclasses click.UsageError, not ValueError.

    Uncaught it exits 2 through Click and never reaches the report, so it must
    be caught by the engine and reported like any other rejection.
    """
    report = run_validation(_project(tmp_path, _MINIMAL + "targets: []\n"))

    assert "manifest.rejected" in _codes(report)
    assert report.exit_code() == 1


def test_conflicting_target_keys_are_a_loader_rejection(tmp_path):
    manifest = _MINIMAL + "target: claude\ntargets:\n  - copilot\n"

    report = run_validation(_project(tmp_path, manifest))

    assert "manifest.rejected" in _codes(report)


def test_a_bad_targets_block_still_renders_a_report_through_the_cli(tmp_path):
    """Exit 1 with a finding, not Click's exit 2 usage error."""
    root = _project(tmp_path, _MINIMAL + "targets: []\n")

    result = CliRunner().invoke(cli, ["validate", str(root), "--format", "json"])

    assert result.exit_code == 1
    assert json.loads(result.output)["findings"][0]["code"] == "manifest.rejected"


# --------------------------------------------------------------------------
# Paths family
# --------------------------------------------------------------------------


def test_missing_local_dependency_is_an_error(tmp_path):
    manifest = _MINIMAL.replace("  apm: []", "  apm:\n    - ./packages/ghost")

    report = run_validation(_project(tmp_path, manifest))

    finding = next(f for f in report.findings if f.code == "paths.local-dependency-missing")
    assert finding.severity is Severity.ERROR
    assert "./packages/ghost" in finding.message
    assert report.exit_code() == 1


def test_present_local_dependency_without_a_package_marker_warns(tmp_path):
    manifest = _MINIMAL.replace("  apm: []", "  apm:\n    - ./packages/bare")
    root = _project(tmp_path, manifest)
    (root / "packages" / "bare").mkdir(parents=True)

    report = run_validation(root)

    assert "paths.local-dependency-not-a-package" in _codes(report)
    assert report.errors == ()


def test_resolvable_local_dependency_produces_no_path_finding(tmp_path):
    manifest = _MINIMAL.replace("  apm: []", "  apm:\n    - ./packages/real")
    root = _project(tmp_path, manifest)
    dep = root / "packages" / "real"
    dep.mkdir(parents=True)
    (dep / "apm.yml").write_text("name: real\nversion: 1.0.0\n", encoding="utf-8")

    report = run_validation(root)

    assert not [f for f in report.findings if f.check == "paths"]


def test_missing_includes_path_is_an_error(tmp_path):
    manifest = _MINIMAL.replace(
        "includes: auto", "includes:\n  - .apm/instructions/\n  - .apm/gone/"
    )

    report = run_validation(_project(tmp_path, manifest))

    finding = next(f for f in report.findings if f.code == "paths.includes-missing")
    assert finding.severity is Severity.ERROR
    assert ".apm/gone/" in finding.message


def test_escaping_includes_path_is_an_error(tmp_path):
    manifest = _MINIMAL.replace("includes: auto", "includes:\n  - ../outside/\n")

    report = run_validation(_project(tmp_path, manifest))

    assert "paths.includes-unsafe" in _codes(report)


def test_absolute_includes_path_is_an_error(tmp_path):
    manifest = _MINIMAL.replace("includes: auto", "includes:\n  - /etc/passwd\n")

    report = run_validation(_project(tmp_path, manifest))

    assert "paths.includes-unsafe" in _codes(report)


def test_includes_auto_enumerates_nothing_to_check(tmp_path):
    report = run_validation(_project(tmp_path))

    assert not [f for f in report.findings if f.check == "paths"]


def test_existing_includes_path_passes(tmp_path):
    manifest = _MINIMAL.replace("includes: auto", "includes:\n  - .apm/instructions/\n")

    report = run_validation(_project(tmp_path, manifest))

    assert not [f for f in report.findings if f.check == "paths"]


# --------------------------------------------------------------------------
# Lockfile family
# --------------------------------------------------------------------------


def test_declared_dependency_without_a_lockfile_warns(tmp_path):
    manifest = _MINIMAL.replace("  apm: []", "  apm:\n    - ./packages/real")
    root = _project(tmp_path, manifest)
    dep = root / "packages" / "real"
    dep.mkdir(parents=True)
    (dep / "apm.yml").write_text("name: real\nversion: 1.0.0\n", encoding="utf-8")

    report = run_validation(root)

    assert "lockfile.not-found" in _codes(report)
    assert report.errors == ()


def test_no_dependencies_and_no_lockfile_is_silent(tmp_path):
    report = run_validation(_project(tmp_path))

    assert not [f for f in report.findings if f.check == "lockfile"]


def test_unreadable_lockfile_is_an_error(tmp_path):
    root = _project(tmp_path)
    (root / "apm.lock.yaml").write_text("lockfile_version: 999\n", encoding="utf-8")

    report = run_validation(root)

    finding = next(f for f in report.findings if f.code == "lockfile.unreadable")
    assert finding.severity is Severity.ERROR


# --------------------------------------------------------------------------
# Family selection
# --------------------------------------------------------------------------


def test_check_selection_limits_reported_families(tmp_path):
    manifest = _MINIMAL.replace("  apm: []", "  apm:\n    - ./packages/ghost")
    root = _project(tmp_path, manifest, with_apm_dir=False)

    report = run_validation(root, checks=("paths",))

    assert report.checks_run == ("paths",)
    assert {f.check for f in report.findings} == {"paths"}


def test_check_selection_keeps_report_order(tmp_path):
    report = run_validation(_project(tmp_path), checks=("lockfile", "manifest"))

    assert report.checks_run == ("manifest", "lockfile")


def test_unknown_check_family_is_a_caller_error(tmp_path):
    with pytest.raises(ValueError, match="Unknown check family"):
        run_validation(_project(tmp_path), checks=("nonsense",))


def test_deselecting_manifest_still_explains_an_unloadable_one(tmp_path):
    (tmp_path / "apm.yml").write_text("- not a mapping\n", encoding="utf-8")

    report = run_validation(tmp_path, checks=("paths",))

    assert "manifest.not-loaded" in _codes(report)
    assert report.exit_code() == 1


# --------------------------------------------------------------------------
# Ordering, reporting and exit codes through the CLI
# --------------------------------------------------------------------------


def test_errors_sort_before_warnings(tmp_path):
    manifest = _MINIMAL.replace("  apm: []", "  apm:\n    - ./packages/ghost").replace(
        "1.0.0", "v1"
    )

    report = run_validation(_project(tmp_path, manifest))

    severities = [f.severity for f in report.findings]
    assert severities == sorted(severities, key=lambda s: 0 if s is Severity.ERROR else 1)


def test_command_exits_one_on_an_error(tmp_path):
    manifest = _MINIMAL.replace("  apm: []", "  apm:\n    - ./packages/ghost")

    result = CliRunner().invoke(cli, ["validate", str(_project(tmp_path, manifest))])

    assert result.exit_code == 1
    assert "paths.local-dependency-missing" in result.output
    assert "Validation failed" in result.output


def test_command_passes_with_warnings_and_advertises_strict(tmp_path):
    result = CliRunner().invoke(
        cli, ["validate", str(_project(tmp_path, _MINIMAL.replace("1.0.0", "v1")))]
    )

    assert result.exit_code == 0
    assert "--strict" in result.output


def test_strict_turns_warnings_into_a_failure(tmp_path):
    root = _project(tmp_path, _MINIMAL.replace("1.0.0", "v1"))

    result = CliRunner().invoke(cli, ["validate", str(root), "--strict"])

    assert result.exit_code == 1
    assert "--strict" in result.output


def test_hints_are_printed_for_findings_that_carry_one(tmp_path):
    manifest = _MINIMAL.replace("  apm: []", "  apm:\n    - ./packages/ghost")

    result = CliRunner().invoke(cli, ["validate", str(_project(tmp_path, manifest))])

    assert "correct the path in apm.yml" in result.output


def test_json_format_is_machine_readable(tmp_path):
    root = _project(tmp_path, _MINIMAL.replace("1.0.0", "v1"))

    result = CliRunner().invoke(cli, ["validate", str(root), "--format", "json"])

    payload = json.loads(result.output)
    assert result.exit_code == 0
    assert payload["ok"] is True
    assert payload["name"] == "demo"
    assert payload["checks"] == list(CHECK_FAMILIES)
    assert payload["summary"]["errors"] == 0
    assert payload["summary"]["warnings"] >= 1
    assert payload["findings"][0]["code"] == "manifest.version-not-semver"


def test_json_format_reflects_strict_in_the_verdict(tmp_path):
    root = _project(tmp_path, _MINIMAL.replace("1.0.0", "v1"))

    result = CliRunner().invoke(cli, ["validate", str(root), "--format", "json", "--strict"])

    payload = json.loads(result.output)
    assert result.exit_code == 1
    assert payload["strict"] is True
    assert payload["ok"] is False


def test_a_manifest_file_can_be_passed_directly(tmp_path):
    root = _project(tmp_path)

    result = CliRunner().invoke(cli, ["validate", str(root / "apm.yml")])

    assert result.exit_code == 0


def test_manifest_is_discovered_from_a_subdirectory(tmp_path):
    root = _project(tmp_path)
    nested = root / "src" / "deep"
    nested.mkdir(parents=True)

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=nested) as _:
        import os

        os.chdir(nested)
        result = runner.invoke(cli, ["validate"])

    assert result.exit_code == 0
    assert "demo" in result.output


# --------------------------------------------------------------------------
# Read-only guarantee
# --------------------------------------------------------------------------


def test_validation_writes_nothing(tmp_path):
    """A read-only command must leave the project byte-identical."""
    manifest = _MINIMAL.replace("  apm: []", "  apm:\n    - ./packages/ghost")
    root = _project(tmp_path, manifest)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    run_validation(root)

    after = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert after == before

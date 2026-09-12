---
title: apm validate
description: Check apm.yml and the project around it offline -- schema conformance, structure, declared paths, and lockfile drift.
sidebar:
  order: 30
---

## Synopsis

```bash
apm validate [<path>] [--strict] [--format text|json] [--check <family>]... [--verbose]
```

## Description

`apm validate` checks a project's manifest and its immediate surroundings
without resolving, installing, or contacting the network. It is read-only: it
writes no files and creates no user configuration, so it is safe in CI, in a
pre-commit hook, or against a directory you do not own.

With no argument it discovers `apm.yml` by walking up from the working
directory. Pass a project directory, or an `apm.yml` file directly, to validate
somewhere else.

## Severity contract

Two levels, with a deliberately narrow split:

| Severity | Meaning | Affects exit code? |
|---|---|---|
| `[x]` error | APM itself cannot load the manifest, or a path the manifest declares definitively does not exist. The project is already broken for `apm install` and `apm pack`. | Yes |
| `[!]` warning | A deviation from the [manifest schema](../../manifest-schema/), or an advisory about project shape, that the current loader tolerates. | Only with `--strict` |

This split protects one invariant: **`apm validate` never fails a project that
`apm install` accepts.** Anything advisory is a warning, so producers can gate
on `--strict` without consumers inheriting a failing build.

## Check families

Each family is independently selectable with `--check`. The default runs all
four.

| Family | What it checks |
|---|---|
| `manifest` | The manifest parses as a YAML mapping and APM's own loader accepts it -- required fields, `$schema` contract, target values, `type`, `includes` form, dependency reference syntax, registry block. Adds spec advisories the loader tolerates: non-semver `version`, unknown top-level keys, absent `description` / `author` / `license`, malformed `scripts`. |
| `structure` | Project shape, via the same gate `apm install` applies to a dependency: `.apm/` contents, primitives, skill bundles, plugin manifests. Always advisory -- a healthy consumer project legitimately ships no primitives of its own. |
| `paths` | Every local-path dependency resolves to a real directory, and every explicit `includes:` entry exists, is not a symlink, and does not escape the project root -- the three rules [`apm pack`](../pack/) enforces. |
| `lockfile` | Direct dependencies declared in `apm.yml` against those locked in `apm.lock.yaml`, both ways. Transitive entries are excluded, since the root manifest never declares them. |

## Finding codes

Codes are stable identifiers in `family.kebab-case` form, safe to match on in
scripts. A code's meaning does not change once released.

| Code | Severity |
|---|---|
| `manifest.not-found`, `manifest.unreadable`, `manifest.invalid-yaml`, `manifest.empty`, `manifest.not-mapping`, `manifest.rejected` | error |
| `manifest.version-not-semver`, `manifest.unknown-key`, `manifest.field-missing`, `manifest.field-type`, `manifest.scripts-not-mapping`, `manifest.script-not-string` | warning |
| `structure.advisory`, `structure.check-failed` | warning |
| `paths.local-dependency-missing`, `paths.local-dependency-unresolvable`, `paths.includes-missing`, `paths.includes-unsafe`, `paths.includes-symlink` | error |
| `paths.local-dependency-not-a-package`, `paths.includes-not-string` | warning |
| `lockfile.unreadable` | error |
| `lockfile.not-found`, `lockfile.declared-not-locked`, `lockfile.locked-not-declared` | warning |

## Options

| Flag | Description |
|---|---|
| `<path>` | Project directory or `apm.yml` file to validate. Default: discovered by walking up from the working directory. |
| `--strict` | Treat warnings as failures. Use this to gate CI. |
| `--format text\|json` | Report format. Default: `text`. |
| `--check <family>` | Limit to one check family; repeatable. Default: all families. |
| `-v`, `--verbose` | Show which check families ran. |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | No errors (and, under `--strict`, no warnings either). |
| 1 | At least one error, or -- under `--strict` -- at least one warning. |

## Examples

Validate the current project:

```bash
apm validate
```

Gate a CI job on full conformance:

```bash
apm validate --strict
```

Check only that declared paths and the lockfile line up:

```bash
apm validate --check paths --check lockfile
```

Machine-readable output for a report or a custom gate:

```bash
apm validate --format json
```

```json
{
  "root": "/work/my-project",
  "manifest": "/work/my-project/apm.yml",
  "name": "my-project",
  "version": "0.1.0",
  "checks": ["manifest", "structure", "paths", "lockfile"],
  "strict": false,
  "ok": true,
  "summary": { "errors": 0, "warnings": 1 },
  "findings": [
    {
      "code": "manifest.field-missing",
      "severity": "warning",
      "check": "manifest",
      "field": "license",
      "message": "No 'license' declared.",
      "hint": "Add 'license:' so consumers and SBOM exports can record it."
    }
  ]
}
```

## See also

- [`apm doctor`](../doctor/) -- diagnose the *environment* rather than the project.
- [`apm audit`](../audit/) -- scan installed primitives for drift and policy violations.
- [Manifest schema](../../manifest-schema/) -- the contract `apm validate` checks against.

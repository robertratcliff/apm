"""Fail closed when the running CLI is older than the lockfile's generator.

APM records the generating CLI in the lockfile's top-level ``apm_version``,
but nothing stopped an OLDER CLI from rewriting a lockfile produced by a
NEWER one (microsoft/apm#2334).  A structurally compatible older CLI would
silently drop whatever lockfile metadata was introduced between the two
versions -- new hash fields, new integrity checks, new constraint syntax --
with no error and no warning.

``require_compatible_lockfile_generator`` is the guard.  It is deliberately
separate from :class:`~apm_cli.deps.lockfile.LockfileFormatError` and its
subclasses so that a CLI-compatibility refusal never reads as a dependency,
cache, or schema failure (issue #2334, acceptance criterion 6).

Version comparison here is intentionally NOT
:func:`apm_cli.utils.version_checker.is_newer_version`.  That helper answers
"should I show an update notification?" and returns ``False`` for anything it
cannot parse, which is the right default for a notifier and precisely the
wrong one for a guard.  It also orders prereleases lexicographically, making
``a10`` sort below ``a9``.

Two asymmetries are deliberate:

* **Unrecognizable RECORDED metadata is fatal**; an unrecognizable RUNNING
  version is not.  A lockfile we cannot read is a reason to stop, but a CLI
  that cannot introspect its own version is a property of how APM was
  packaged, not of the user's project.  PyInstaller binaries carry no
  distribution metadata, so :func:`running_cli_version` resolves through
  :func:`apm_cli.version.get_version` (build-time constant, then distribution
  metadata, then ``pyproject.toml``) and a residual failure degrades to
  :class:`UndeterminableRunningVersionError`, which callers surface as a
  warning rather than a refusal.  Blocking every mutating command on those
  builds would be a worse regression than the one this guard prevents.
* **``--dry-run`` warns instead of refusing.**  A dry run writes nothing, and
  it is the one command a blocked user reaches for to understand what would
  happen.  See ``dry_run`` on :func:`enforce_or_exit`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

# APM's own sentinel for "the running distribution could not identify itself"
# (see ``apm_cli.deps.lockfile.installed_apm_version`` and
# ``apm_cli.version.get_version``).  APM writes this into real lockfiles, so
# it must be treated as absent metadata rather than as corruption -- refusing
# it would brick lockfiles APM itself produced.
UNKNOWN_VERSION = "unknown"

# Supported grammar (a pragmatic PEP 440 subset covering everything APM has
# released or can build):
#
#     N.N.N[<sep>(a|alpha|b|beta|rc|c)N][<sep>postN][<sep>devN][+local]
#
# where <sep> is an optional ``.``, ``-`` or ``_``.  Anything outside this --
# an epoch (``1!2.0.0``), a four-segment release, a bare ``1.2``, a ``v``
# prefix (APM tags carry one, recorded apm_version never does) -- returns
# ``None`` and is handled per the recorded/running asymmetry documented above.
_VERSION_RE = re.compile(
    r"""^
    (\d+)\.(\d+)\.(\d+)
    (?:[._-]?(a|alpha|b|beta|rc|c)\.?(\d+))?
    (?:[._-]?post\.?(\d+))?
    (?:[._-]?dev\.?(\d+))?
    $""",
    re.VERBOSE | re.IGNORECASE,
)

# Prerelease precedence: a < b < rc < stable.  ``alpha``/``beta``/``c`` are
# alternate PEP 440 spellings of ``a``/``b``/``rc``.
_PRERELEASE_RANK = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "rc": 2, "c": 2}
_STABLE_RANK = 3
# PEP 440: a bare ``X.Y.Z.devN`` -- no prerelease and no post segment -- is a
# dev build of the FINAL release, and sorts below every prerelease of it
# (``1.0.dev1 < 1.0a1.dev1 < 1.0a1 < 1.0``).  Absent-prerelease therefore
# sorts lowest in that one case and highest in every other.
_PRE_DEV_OF_FINAL_RANK = -1

# A ``.devN`` build precedes the release it builds toward, so absent-dev must
# sort ABOVE any dev number at the same release level.
_DEV_PRESENT = 0
_DEV_ABSENT = 1

# Tags and key name used when walking the composed YAML node tree.  The field
# is read from the node tree rather than from the raw file text: a text-level
# match recognises exactly one spelling of the key and silently disables the
# guard on every other valid one (``"apm_version":`` quoted, a BOM-prefixed
# file), which fails OPEN -- the single direction #2334 asks the guard never
# to fail in.  The node tree also preserves the scalar verbatim, so a value
# like ``1.10`` is compared as written instead of being coerced to ``1.1`` by
# YAML's float resolver.
_YAML_STR_TAG = "tag:yaml.org,2002:str"
_YAML_MERGE_TAG = "tag:yaml.org,2002:merge"
_GENERATOR_KEY = "apm_version"

VersionKey = tuple[int, int, int, int, int, int, int, int]


class LockfileGeneratorError(Exception):
    """Base class for CLI-vs-lockfile generator compatibility refusals."""


class StaleCliError(LockfileGeneratorError):
    """The running CLI is older than the CLI that generated the lockfile."""

    def __init__(self, running: str, recorded: str, hint: str) -> None:
        self.running = running
        self.recorded = recorded
        super().__init__(
            f"This lockfile was generated by APM {recorded}, but the running "
            f"CLI is APM {running}.\n"
            f"Refusing to rewrite a lockfile produced by a newer APM -- doing so "
            f"would silently drop any lockfile data {running} does not understand.\n"
            f"{hint}, then re-run this command."
        )


class UnsafeGeneratorMetadataError(LockfileGeneratorError):
    """The recorded generator metadata cannot be trusted to compare against."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(
            f"Refusing to rewrite this lockfile: {detail}\n"
            "The recorded apm_version cannot be compared against the running "
            "CLI, so APM cannot prove this rewrite is safe. Repair or remove "
            "apm.lock.yaml and re-run to regenerate it."
        )


class UndeterminableRunningVersionError(LockfileGeneratorError):
    """This build of APM cannot report a version comparable to the lockfile's.

    Advisory, not fatal.  Raised when the lockfile names a specific generator
    but the running CLI cannot identify itself -- a property of how APM was
    packaged, not of the user's project.  :func:`enforce_or_exit` renders this
    as a warning and continues.
    """

    def __init__(self, running: str, recorded: str) -> None:
        self.running = running
        self.recorded = recorded
        super().__init__(
            f"This build of APM reports its version as {running!r}, so it cannot be "
            f"compared against the lockfile's recorded generator {recorded}.\n"
            f"Continuing anyway. If this APM is older than {recorded}, this command "
            f"may drop lockfile data it does not understand -- check 'apm --version' "
            f"against the recorded generator before committing apm.lock.yaml."
        )


def parse_version(raw: object) -> VersionKey | None:
    """Return an orderable key for *raw*, or ``None`` when it is not a version.

    Build metadata (``+local``) is stripped before comparison so it never
    affects precedence.  Numeric segments compare numerically, so ``0.1.0a9``
    correctly precedes ``0.1.0a10``.  Ordering across suffixes follows
    PEP 440::

        0.25.0.dev1 < 0.25.0a1 < 0.25.0b1 < 0.25.0rc1 < 0.25.0 < 0.25.0.post1
    """
    if not isinstance(raw, str):
        return None
    # A local/build segment is metadata, not precedence (PEP 440 / semver).
    text = raw.strip().split("+", 1)[0]
    match = _VERSION_RE.match(text)
    if not match:
        return None
    major, minor, patch, prerelease, prerelease_num, post_num, dev_num = match.groups()
    if prerelease is not None:
        pre_rank = _PRERELEASE_RANK[prerelease.lower()]
        pre_num = int(prerelease_num)
    elif post_num is None and dev_num is not None:
        # A dev build of the final release; see _PRE_DEV_OF_FINAL_RANK.
        pre_rank, pre_num = _PRE_DEV_OF_FINAL_RANK, 0
    else:
        pre_rank, pre_num = _STABLE_RANK, 0
    return (
        int(major),
        int(minor),
        int(patch),
        pre_rank,
        pre_num,
        int(post_num or 0),
        _DEV_ABSENT if dev_num is None else _DEV_PRESENT,
        int(dev_num or 0),
    )


def running_cli_version() -> str:
    """Return the running CLI's version, or ``UNKNOWN_VERSION``.

    Prefers :func:`apm_cli.version.get_version`, which consults the
    PyInstaller build-time constant first and falls back to distribution
    metadata and then ``pyproject.toml``.  ``installed_apm_version`` -- which
    only ever asks ``importlib.metadata`` -- is the last resort: it is what
    stamps the lockfile, but it reports ``unknown`` from a frozen binary,
    where no distribution metadata is shipped.

    This is the single seam to patch when simulating a given CLI version.
    """
    try:
        from ..version import get_version

        candidate = get_version()
    except Exception:  # pragma: no cover - defensive; get_version swallows its own
        candidate = None
    if isinstance(candidate, str) and candidate and candidate != UNKNOWN_VERSION:
        return candidate

    from .lockfile import installed_apm_version

    return installed_apm_version()


def read_recorded_generator(lockfile_path: Path) -> str | None:
    """Return the lockfile's recorded ``apm_version``.

    Returns ``None`` when there is no lockfile, when it predates the
    ``apm_version`` field, or when it is unreadable -- none of which are this
    guard's failure to report.

    The field is located in the composed YAML node tree, so every valid
    spelling of the key resolves to the same field and only a key in the
    top-level mapping is considered; a nested ``apm_version`` still belongs to
    someone else's mapping and is ignored.  Raises
    :class:`UnsafeGeneratorMetadataError` when the file cannot be reduced to a
    single knowable generator: the key declared more than once (YAML would
    silently resolve the duplicate to the last value), a non-scalar value, or
    a top-level merge key that could contribute a generator this function does
    not expand.
    """
    try:
        text = lockfile_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    import yaml

    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        # A lockfile that will not parse is the schema layer's failure to
        # report, not this guard's.
        return None
    if not isinstance(root, yaml.MappingNode):
        return None

    recorded_nodes = [
        value_node
        for key_node, value_node in root.value
        if isinstance(key_node, yaml.ScalarNode)
        and key_node.tag == _YAML_STR_TAG
        and key_node.value == _GENERATOR_KEY
    ]
    if len(recorded_nodes) > 1:
        raise UnsafeGeneratorMetadataError(
            f"apm.lock.yaml declares a top-level apm_version {len(recorded_nodes)} times"
        )
    if not recorded_nodes:
        # An explicit key wins over anything a merge key would contribute, so
        # this is the only case where an unexpanded ``<<`` can hide a
        # generator. Refuse rather than proceed: an unreadable generator is
        # exactly what UnsafeGeneratorMetadataError exists to report.
        if any(
            isinstance(key_node, yaml.ScalarNode) and key_node.tag == _YAML_MERGE_TAG
            for key_node, _ in root.value
        ):
            raise UnsafeGeneratorMetadataError(
                "apm.lock.yaml merges an anchor into its top-level mapping, which may "
                "contribute an apm_version that cannot be read directly"
            )
        return None

    recorded = recorded_nodes[0]
    if not isinstance(recorded, yaml.ScalarNode):
        raise UnsafeGeneratorMetadataError(
            "apm.lock.yaml records a top-level apm_version that is not a single value"
        )
    return recorded.value


def require_compatible_lockfile_generator(
    lockfile_path: Path,
    *,
    running_version: str | None = None,
) -> None:
    """Refuse to continue when the running CLI predates the lockfile generator.

    Call this BEFORE a mutating command does any work, so a refusal leaves the
    lockfile -- and everything else -- byte-for-byte unchanged.  A missing
    lockfile, a pre-``apm_version`` lockfile, and the ``unknown`` sentinel are
    all no-ops: there is nothing to compare against.

    Raises:
        StaleCliError: The running CLI is strictly older than the generator.
        UnsafeGeneratorMetadataError: The recorded metadata is duplicated or
            malformed.
        UndeterminableRunningVersionError: The lockfile names a specific
            generator but this build of APM cannot report a comparable
            version.  Advisory -- see the class docstring.
    """
    recorded_raw = read_recorded_generator(lockfile_path)
    if recorded_raw is None or recorded_raw == UNKNOWN_VERSION:
        return

    recorded = parse_version(recorded_raw)
    if recorded is None:
        raise UnsafeGeneratorMetadataError(
            f"apm.lock.yaml records an unrecognizable apm_version {recorded_raw!r}"
        )

    if running_version is None:
        running_version = running_cli_version()

    running = parse_version(running_version)
    if running is None:
        raise UndeterminableRunningVersionError(running_version, recorded_raw)

    if running < recorded:
        from ..update_policy import get_update_hint_message

        raise StaleCliError(running_version, recorded_raw, get_update_hint_message())


def enforce_or_exit(
    lockfile_path: Path,
    *,
    error: Callable[[str], object],
    warn: Callable[[str], object] | None = None,
    dry_run: bool = False,
    running_version: str | None = None,
) -> None:
    """Guard *lockfile_path*, rendering any refusal through *error* and exiting 1.

    The CLI-facing wrapper around
    :func:`require_compatible_lockfile_generator`.  *error* is the calling
    command's own error renderer (``logger.error``, ``_rich_error``, ...), so
    this stays free of any presentation-layer import.  *warn* is the matching
    warning renderer; it falls back to *error* when the caller has none.

    Call it at the top of a mutating command, before the command does any
    work, so a refusal leaves the project untouched.

    Args:
        dry_run: When true, a refusal is downgraded to a warning and the
            command continues.  ``--dry-run`` writes nothing, and it is what a
            blocked user runs to find out what the real command would do;
            refusing it removes that diagnostic for no protective benefit.
    """
    import sys

    render_warning = warn if warn is not None else error

    try:
        require_compatible_lockfile_generator(
            lockfile_path,
            running_version=running_version,
        )
    except UndeterminableRunningVersionError as exc:
        # Advisory only: this build cannot introspect its own version.
        render_warning(str(exc))
    except LockfileGeneratorError as exc:
        if dry_run:
            render_warning(f"{exc}\n(--dry-run writes nothing, so this run continues.)")
            return
        error(str(exc))
        sys.exit(1)

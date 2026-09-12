"""Offline validation engine for APM projects.

Public surface:

* :func:`run_validation` -- validate a project and get a structured report
* :class:`ValidationReport`, :class:`Finding`, :class:`Severity`
* :data:`CHECK_FAMILIES` -- selectable check families

The engine is read-only and network-free; see :mod:`apm_cli.validation.checks`.
"""

from .model import CHECK_FAMILIES, Finding, Severity, ValidationReport
from .runner import run_validation

__all__ = [
    "CHECK_FAMILIES",
    "Finding",
    "Severity",
    "ValidationReport",
    "run_validation",
]

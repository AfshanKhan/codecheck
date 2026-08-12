"""Collects all house rule checks."""

from __future__ import annotations

from codecheck.checks.base import HouseCheck
from codecheck.checks.no_bare_except import NoBareExceptCheck
from codecheck.checks.no_sql_string_format import NoSqlStringFormatCheck

ALL_CHECKS: list[HouseCheck] = [
    NoBareExceptCheck(),
    NoSqlStringFormatCheck(),
]

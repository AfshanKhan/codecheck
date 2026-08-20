"""Collects all house rule checks."""

from __future__ import annotations

from codecheck.checks.base import HouseCheck
from codecheck.checks.hardcoded_credential import HardcodedCredentialCheck
from codecheck.checks.js_console_debugger import JsConsoleLogCheck, JsDebuggerStatementCheck
from codecheck.checks.js_frappe_call_error_handling import JsFrappeCallErrorHandlingCheck
from codecheck.checks.js_hardcoded_credential import JsHardcodedCredentialCheck
from codecheck.checks.js_hardcoded_html import JsHardcodedHtmlCheck
from codecheck.checks.js_inline_style import JsInlineStyleCheck
from codecheck.checks.js_jquery_dom import JsJqueryDomCheck
from codecheck.checks.leftover_print import LeftoverPrintCheck
from codecheck.checks.method_too_long import MethodTooLongCheck
from codecheck.checks.missing_translation import MissingTranslationCheck
from codecheck.checks.n_plus_one_query import NPlusOneQueryCheck
from codecheck.checks.no_bare_except import NoBareExceptCheck
from codecheck.checks.no_manual_commit import NoManualCommitCheck
from codecheck.checks.no_sql_string_format import NoSqlStringFormatCheck
from codecheck.checks.silent_exception import SilentExceptionCheck
from codecheck.checks.whitelist_permission_check import WhitelistPermissionCheck

ALL_CHECKS: list[HouseCheck] = [
    NoBareExceptCheck(),
    NoSqlStringFormatCheck(),
    WhitelistPermissionCheck(),
    NPlusOneQueryCheck(),
    NoManualCommitCheck(),
    MissingTranslationCheck(),
    LeftoverPrintCheck(),
    SilentExceptionCheck(),
    HardcodedCredentialCheck(),
    JsHardcodedHtmlCheck(),
    JsInlineStyleCheck(),
    JsConsoleLogCheck(),
    JsDebuggerStatementCheck(),
    JsJqueryDomCheck(),
    JsFrappeCallErrorHandlingCheck(),
    JsHardcodedCredentialCheck(),
    MethodTooLongCheck(),
]

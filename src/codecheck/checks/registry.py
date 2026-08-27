"""Collects all house rule checks."""

from __future__ import annotations

import importlib

from codecheck.checks.base import HouseCheck
from codecheck.checks.blocking_call_in_doc_event import (
    BlockingHttpCallInDocEventCheck,
    SyncPdfGenerationInDocEventCheck,
)
from codecheck.checks.boolean_flag_param import BooleanFlagParamCheck
from codecheck.checks.commented_code import CommentedOutJsCodeCheck, CommentedOutPythonCodeCheck
from codecheck.checks.doctype_schema import DoctypeJsonBlobFieldCheck, DoctypeJsonSyntaxCheck
from codecheck.checks.hardcoded_credential import HardcodedCredentialCheck
from codecheck.checks.hooks_structure import DoctypeClassOverrideCheck, HooksPyDeclarativeCheck
from codecheck.checks.js_async_await_suggestion import JsAsyncAwaitSuggestionCheck
from codecheck.checks.js_client_script_length import JsClientScriptLengthCheck
from codecheck.checks.js_console_debugger import JsConsoleLogCheck, JsDebuggerStatementCheck
from codecheck.checks.js_direct_dom import JsDirectDomCheck
from codecheck.checks.js_frappe_call_error_handling import JsFrappeCallErrorHandlingCheck
from codecheck.checks.js_hardcoded_credential import JsHardcodedCredentialCheck
from codecheck.checks.js_hardcoded_html import JsHardcodedHtmlCheck
from codecheck.checks.js_inline_style import JsInlineStyleCheck
from codecheck.checks.js_jquery_dom import JsJqueryDomCheck
from codecheck.checks.leftover_print import LeftoverPrintCheck
from codecheck.checks.magic_number import MagicNumberCheck
from codecheck.checks.method_too_long import MethodTooLongCheck
from codecheck.checks.missing_docstring import MissingDocstringCheck
from codecheck.checks.missing_translation import MissingTranslationCheck
from codecheck.checks.n_plus_one_query import NPlusOneQueryCheck
from codecheck.checks.no_bare_except import NoBareExceptCheck
from codecheck.checks.no_manual_commit import NoManualCommitCheck
from codecheck.checks.no_sql_string_format import NoSqlStringFormatCheck
from codecheck.checks.save_in_loop import SaveInLoopCheck
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
    HooksPyDeclarativeCheck(),
    DoctypeClassOverrideCheck(),
    DoctypeJsonSyntaxCheck(),
    DoctypeJsonBlobFieldCheck(),
    BlockingHttpCallInDocEventCheck(),
    SyncPdfGenerationInDocEventCheck(),
    SaveInLoopCheck(),
    JsAsyncAwaitSuggestionCheck(),
    JsDirectDomCheck(),
    JsClientScriptLengthCheck(),
    CommentedOutPythonCodeCheck(),
    CommentedOutJsCodeCheck(),
    MagicNumberCheck(),
    MissingDocstringCheck(),
    BooleanFlagParamCheck(),
]


def load_extra_checks(dotted_paths: list[str]) -> tuple[list[HouseCheck], list[str]]:
    """Dynamically imports and instantiates each "module.submodule:ClassName"
    path, for `rules.extra_checks` in config.yaml -- lets a project add its
    own HouseCheck subclasses without forking codecheck to add them to
    ALL_CHECKS directly. Returns (loaded_checks, error_messages); a path that
    fails to import, isn't a HouseCheck subclass, or can't be instantiated
    with no arguments is reported as an error string rather than raised, so
    one bad entry doesn't take down every built-in check along with it.
    """
    checks: list[HouseCheck] = []
    errors: list[str] = []
    for dotted_path in dotted_paths:
        module_name, _, class_name = dotted_path.partition(":")
        if not module_name or not class_name:
            errors.append(f"{dotted_path}: expected 'module.path:ClassName'")
            continue
        try:
            module = importlib.import_module(module_name)
            check_class = getattr(module, class_name)
        except (ImportError, AttributeError) as e:
            errors.append(f"{dotted_path}: {e}")
            continue
        if not (isinstance(check_class, type) and issubclass(check_class, HouseCheck)):
            errors.append(f"{dotted_path}: not a HouseCheck subclass")
            continue
        try:
            checks.append(check_class())
        except Exception as e:  # a broken __init__ shouldn't crash the whole run
            errors.append(f"{dotted_path}: failed to instantiate: {e}")
    return checks, errors

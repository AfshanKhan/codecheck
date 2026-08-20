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
from codecheck.models import Severity


def test_bare_except_flagged_on_changed_line():
    content = "try:\n    pass\nexcept:\n    pass\n"
    findings = NoBareExceptCheck().check_file("a.py", content, changed_lines={3})
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-001"
    assert findings[0].line_start == 3


def test_bare_except_not_flagged_outside_changed_lines():
    content = "try:\n    pass\nexcept:\n    pass\n"
    findings = NoBareExceptCheck().check_file("a.py", content, changed_lines={1})
    assert findings == []


def test_typed_except_not_flagged():
    content = "try:\n    pass\nexcept ValueError:\n    pass\n"
    findings = NoBareExceptCheck().check_file("a.py", content, changed_lines={1, 2, 3, 4})
    assert findings == []


def test_sql_fstring_flagged():
    content = 'name = "x"\nfrappe.db.sql(f"select * from tab where name = {name}")\n'
    findings = NoSqlStringFormatCheck().check_file("a.py", content, changed_lines={2})
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-002"


def test_sql_percent_format_flagged():
    content = 'frappe.db.sql("select * from tab where name = %s" % name)\n'
    findings = NoSqlStringFormatCheck().check_file("a.py", content, changed_lines={1})
    assert len(findings) == 1


def test_sql_parameterized_not_flagged():
    content = 'frappe.db.sql("select * from tab where name = %s", (name,))\n'
    findings = NoSqlStringFormatCheck().check_file("a.py", content, changed_lines={1})
    assert findings == []


def test_sql_fstring_with_escape_downgraded_to_medium():
    content = 'frappe.db.sql(f"select * from tab where name = {frappe.db.escape(name)}")\n'
    findings = NoSqlStringFormatCheck().check_file("a.py", content, changed_lines={1})
    assert len(findings) == 1
    assert findings[0].severity == Severity.MEDIUM


def test_whitelist_without_permission_check_flagged():
    content = (
        "@frappe.whitelist()\n"
        "def do_thing():\n"
        "    return 1\n"
    )
    findings = WhitelistPermissionCheck().check_file("a.py", content, changed_lines={1, 2, 3})
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-003"


def test_whitelist_with_check_permission_not_flagged():
    content = (
        "@frappe.whitelist()\n"
        "def do_thing(name):\n"
        "    bp = frappe.get_doc('Blanket Order', name)\n"
        "    bp.check_permission('write')\n"
        "    return 1\n"
    )
    findings = WhitelistPermissionCheck().check_file("a.py", content, changed_lines=None)
    assert findings == []


def test_whitelist_allow_guest_not_flagged():
    content = (
        "@frappe.whitelist(allow_guest=True)\n"
        "def public_endpoint():\n"
        "    return 1\n"
    )
    findings = WhitelistPermissionCheck().check_file("a.py", content, changed_lines=None)
    assert findings == []


def test_whitelist_permission_check_in_uncalled_nested_helper_still_flagged():
    # regression (Greptile): a permission check inside a nested function that's
    # never invoked must not count as protecting the outer whitelisted endpoint.
    content = (
        "@frappe.whitelist()\n"
        "def do_thing(name):\n"
        "    def _unused_helper():\n"
        "        frappe.get_doc('Blanket Order', name).check_permission('write')\n"
        "    return 1\n"
    )
    findings = WhitelistPermissionCheck().check_file("a.py", content, changed_lines=None)
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-003"


def test_whitelist_permission_check_via_called_helper_still_not_flagged():
    content = (
        "@frappe.whitelist()\n"
        "def do_thing(name):\n"
        "    bp = frappe.get_doc('Blanket Order', name)\n"
        "    bp.check_permission('write')\n"
        "    return _format(bp)\n"
    )
    findings = WhitelistPermissionCheck().check_file("a.py", content, changed_lines=None)
    assert findings == []


def test_whitelist_permission_check_in_called_nested_helper_not_flagged():
    # regression (Greptile): a permission check inside a nested helper that IS
    # invoked must still count as protecting the outer whitelisted endpoint --
    # the fix for the "uncalled helper" false negative above must not
    # overcorrect into ignoring every nested helper regardless of whether it's
    # actually called.
    content = (
        "@frappe.whitelist()\n"
        "def do_thing(name):\n"
        "    def _check():\n"
        "        frappe.get_doc('Blanket Order', name).check_permission('write')\n"
        "    _check()\n"
        "    return 1\n"
    )
    findings = WhitelistPermissionCheck().check_file("a.py", content, changed_lines=None)
    assert findings == []


def test_whitelist_permission_check_in_transitively_called_helper_not_flagged():
    content = (
        "@frappe.whitelist()\n"
        "def do_thing(name):\n"
        "    def _inner():\n"
        "        frappe.get_doc('Blanket Order', name).check_permission('write')\n"
        "    def _outer():\n"
        "        _inner()\n"
        "    _outer()\n"
        "    return 1\n"
    )
    findings = WhitelistPermissionCheck().check_file("a.py", content, changed_lines=None)
    assert findings == []


def test_whitelist_permission_check_shadowed_name_resolves_to_nearest_scope_protected():
    # regression (Greptile): two helpers share the name "_check" at different
    # nesting depths. The call site is inside do_thing's own scope, so it must
    # resolve to do_thing's own (protected) "_check", not the deeper, unused,
    # unprotected "_check" nested inside _outer -- a flat name->def map that
    # lets the deeper definition overwrite the shallower one gets this wrong.
    content = (
        "@frappe.whitelist()\n"
        "def do_thing(name):\n"
        "    def _check():\n"
        "        frappe.get_doc('Blanket Order', name).check_permission('write')\n"
        "    def _outer():\n"
        "        def _check():\n"
        "            pass\n"
        "        _unrelated_call()\n"
        "    _check()\n"
        "    _outer()\n"
        "    return 1\n"
    )
    findings = WhitelistPermissionCheck().check_file("a.py", content, changed_lines=None)
    assert findings == []


def test_whitelist_permission_check_shadowed_name_resolves_to_nearest_scope_unprotected():
    # inverse of the above: the call site is inside _outer's own scope, so it
    # must resolve to _outer's own (unprotected) "_check", not do_thing's
    # protected one -- do_thing's "_check" is never actually called.
    content = (
        "@frappe.whitelist()\n"
        "def do_thing(name):\n"
        "    def _check():\n"
        "        frappe.get_doc('Blanket Order', name).check_permission('write')\n"
        "    def _outer():\n"
        "        def _check():\n"
        "            pass\n"
        "        _check()\n"
        "    _outer()\n"
        "    return 1\n"
    )
    findings = WhitelistPermissionCheck().check_file("a.py", content, changed_lines=None)
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-003"


def test_n_plus_one_query_flagged_inside_loop():
    content = (
        "for name in names:\n"
        "    doc = frappe.get_doc('Item', name)\n"
    )
    findings = NPlusOneQueryCheck().check_file("a.py", content, changed_lines={2})
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-004"


def test_query_outside_loop_not_flagged():
    content = "doc = frappe.get_doc('Item', name)\n"
    findings = NPlusOneQueryCheck().check_file("a.py", content, changed_lines={1})
    assert findings == []


def test_manual_commit_flagged():
    content = "frappe.db.commit()\n"
    findings = NoManualCommitCheck().check_file("a.py", content, changed_lines={1})
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-005"


def test_untranslated_throw_flagged():
    content = 'frappe.throw("Something went wrong")\n'
    findings = MissingTranslationCheck().check_file("a.py", content, changed_lines={1})
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-006"


def test_translated_throw_not_flagged():
    content = 'frappe.throw(_("Something went wrong"))\n'
    findings = MissingTranslationCheck().check_file("a.py", content, changed_lines={1})
    assert findings == []


def test_leftover_print_flagged():
    content = 'print("debug")\n'
    findings = LeftoverPrintCheck().check_file("a.py", content, changed_lines={1})
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-007"


def test_silent_exception_flagged():
    content = "try:\n    pass\nexcept ValueError:\n    pass\n"
    findings = SilentExceptionCheck().check_file("a.py", content, changed_lines={1, 2, 3, 4})
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-008"


def test_exception_with_handling_not_flagged():
    content = "try:\n    pass\nexcept ValueError:\n    frappe.log_error('oops')\n"
    findings = SilentExceptionCheck().check_file("a.py", content, changed_lines={1, 2, 3, 4})
    assert findings == []


def test_hardcoded_credential_flagged():
    content = 'api_key = "sk-abc123realvalue"\n'
    findings = HardcodedCredentialCheck().check_file("a.py", content, changed_lines={1})
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-009"


def test_hardcoded_credential_placeholder_not_flagged():
    content = 'api_key = "changeme"\n'
    findings = HardcodedCredentialCheck().check_file("a.py", content, changed_lines={1})
    assert findings == []


def test_js_hardcoded_html_flagged():
    content = 'frm.$wrapper.append("<button>Click</button>");\n'
    findings = JsHardcodedHtmlCheck().check_file("a.js", content, changed_lines={1})
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-010"


def test_js_inline_style_flagged():
    content = '$el.html("<div style=\'color:red\'></div>");\n'
    findings = JsInlineStyleCheck().check_file("a.js", content, changed_lines={1})
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-011"


def test_js_console_log_flagged():
    content = "console.log('debug');\n"
    findings = JsConsoleLogCheck().check_file("a.js", content, changed_lines={1})
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-012"


def test_js_debugger_flagged():
    content = "debugger;\n"
    findings = JsDebuggerStatementCheck().check_file("a.js", content, changed_lines={1})
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-013"


def test_js_jquery_dom_flagged():
    content = "$('#some-id').hide();\n"
    findings = JsJqueryDomCheck().check_file("a.js", content, changed_lines={1})
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-014"


def test_js_jquery_dom_safe_wrapper_not_flagged():
    content = "$wrapper.find('.btn').hide();\n"
    findings = JsJqueryDomCheck().check_file("a.js", content, changed_lines={1})
    assert findings == []


def test_js_frappe_call_no_error_handling_flagged():
    content = "frappe.call({\n    method: 'my.method',\n});\n"
    findings = JsFrappeCallErrorHandlingCheck().check_file("a.js", content, changed_lines={1})
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-015"


def test_js_frappe_call_with_error_handling_not_flagged():
    content = "frappe.call({\n    method: 'my.method',\n    error: (r) => console.log(r),\n});\n"
    findings = JsFrappeCallErrorHandlingCheck().check_file("a.js", content, changed_lines={1})
    assert findings == []


def test_js_frappe_call_multiline_chained_style_flagged():
    # regression: found via a live comparison against frappe-pr-reviewer --
    # Prettier-formatted code often breaks "frappe.call(" across two lines
    # ("frappe" then ".call({" on the next line), which the original
    # same-line-only regex never matched.
    content = "frappe\n\t.call({\n\t\tmethod: 'my.method',\n\t});\n"
    findings = JsFrappeCallErrorHandlingCheck().check_file("a.js", content, changed_lines={1})
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-015"
    assert findings[0].line_start == 1


def test_js_hardcoded_credential_flagged():
    content = "const api_key = 'sk-abc123realvalue';\n"
    findings = JsHardcodedCredentialCheck().check_file("a.js", content, changed_lines={1})
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-016"


def test_method_too_long_flagged():
    content = "def big():\n" + "\n".join(f"    x{i} = {i}" for i in range(55)) + "\n    return x0\n"
    findings = MethodTooLongCheck().check_file("a.py", content, changed_lines={1})
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-018"


def test_method_under_limit_not_flagged():
    content = "def small():\n    return 1\n"
    findings = MethodTooLongCheck().check_file("a.py", content, changed_lines={1})
    assert findings == []


def test_async_method_too_long_flagged():
    content = "async def big():\n" + "\n".join(f"    x{i} = {i}" for i in range(55)) + "\n"
    findings = MethodTooLongCheck().check_file("a.py", content, changed_lines={1})
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-018"

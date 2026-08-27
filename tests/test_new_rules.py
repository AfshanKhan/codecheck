"""Tests for RULE-020 through RULE-035 -- ported/adapted from a separate
Frappe audit tool the user maintains, reimplemented from scratch to fit
codecheck's HouseCheck/SubRunner architecture rather than copied.
"""

from codecheck.checks.blocking_call_in_doc_event import (
    BlockingHttpCallInDocEventCheck,
    SyncPdfGenerationInDocEventCheck,
)
from codecheck.checks.boolean_flag_param import BooleanFlagParamCheck
from codecheck.checks.commented_code import CommentedOutJsCodeCheck, CommentedOutPythonCodeCheck
from codecheck.checks.doctype_schema import DoctypeJsonBlobFieldCheck, DoctypeJsonSyntaxCheck
from codecheck.checks.hooks_structure import DoctypeClassOverrideCheck, HooksPyDeclarativeCheck
from codecheck.checks.js_async_await_suggestion import JsAsyncAwaitSuggestionCheck
from codecheck.checks.js_client_script_length import JsClientScriptLengthCheck
from codecheck.checks.js_direct_dom import JsDirectDomCheck
from codecheck.checks.magic_number import MagicNumberCheck
from codecheck.checks.missing_docstring import MissingDocstringCheck
from codecheck.checks.save_in_loop import SaveInLoopCheck
from codecheck.checks.secrets_in_repo import SecretsInRepoRunner
from codecheck.models import ReviewTarget

_JSON_PATH = "my_app/doctype/sales_order/sales_order.json"


# RULE-020: hooks.py must stay declarative
def test_hooks_py_flags_module_level_function():
    content = "app_name = 'x'\n\ndef helper():\n    return 1\n"
    findings = HooksPyDeclarativeCheck().check_file("my_app/hooks.py", content, None)
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-020"
    assert findings[0].line_start == 3


def test_hooks_py_pure_config_not_flagged():
    content = "app_name = 'x'\nimport os\nfixtures = ['DocType']\n"
    assert HooksPyDeclarativeCheck().check_file("my_app/hooks.py", content, None) == []


def test_hooks_py_check_ignores_non_hooks_files():
    content = "def helper():\n    return 1\n"
    assert HooksPyDeclarativeCheck().check_file("my_app/other.py", content, None) == []


# RULE-021: avoid overriding DocType class in hooks.py
def test_doctype_override_flagged():
    content = 'override_doctype_class = {"Sales Order": "my_app.SalesOrder"}\n'
    findings = DoctypeClassOverrideCheck().check_file("my_app/hooks.py", content, None)
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-021"


def test_doctype_override_empty_dict_not_flagged():
    content = "override_doctype_class = {}\n"
    assert DoctypeClassOverrideCheck().check_file("my_app/hooks.py", content, None) == []


# RULE-022: DocType JSON must be valid JSON
def test_invalid_doctype_json_flagged():
    findings = DoctypeJsonSyntaxCheck().check_file(_JSON_PATH, "not valid json", None)
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-022"


def test_valid_doctype_json_not_flagged():
    content = '{"name": "Sales Order", "fields": []}'
    assert DoctypeJsonSyntaxCheck().check_file(_JSON_PATH, content, None) == []


def test_non_doctype_json_ignored():
    assert DoctypeJsonSyntaxCheck().check_file("my_app/package.json", "not json", None) == []


# RULE-023: JSON blob stored in a text field's default value
def test_json_blob_default_flagged():
    content = (
        '{"name": "Sales Order", "fields": '
        '[{"fieldname": "notes", "fieldtype": "Text", "default": "{\\"a\\": 1}"}]}'
    )
    findings = DoctypeJsonBlobFieldCheck().check_file(_JSON_PATH, content, None)
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-023"


def test_plain_default_not_flagged():
    content = (
        '{"name": "Sales Order", "fields": '
        '[{"fieldname": "notes", "fieldtype": "Text", "default": "hello"}]}'
    )
    assert DoctypeJsonBlobFieldCheck().check_file(_JSON_PATH, content, None) == []


def test_json_blob_check_skips_invalid_json_silently():
    # RULE-022 already reports invalid JSON -- this check just backs off.
    assert DoctypeJsonBlobFieldCheck().check_file(_JSON_PATH, "not json", None) == []


# RULE-024: blocking network call inside a doc-event hook
def test_blocking_http_call_in_doc_event_flagged():
    content = (
        "class SalesOrder:\n"
        "    def validate(self):\n"
        "        requests.get('https://example.com')\n"
    )
    findings = BlockingHttpCallInDocEventCheck().check_file("a.py", content, None)
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-024"
    assert findings[0].line_start == 3


def test_http_call_outside_doc_event_not_flagged():
    content = (
        "class SalesOrder:\n"
        "    def some_helper(self):\n"
        "        requests.get('https://example.com')\n"
    )
    assert BlockingHttpCallInDocEventCheck().check_file("a.py", content, None) == []


def test_http_call_in_nested_uncalled_helper_not_flagged():
    content = (
        "class SalesOrder:\n"
        "    def validate(self):\n"
        "        def inner():\n"
        "            requests.get('https://example.com')\n"
    )
    assert BlockingHttpCallInDocEventCheck().check_file("a.py", content, None) == []


# RULE-025: synchronous PDF generation inside a doc-event hook
def test_sync_pdf_generation_in_doc_event_flagged():
    content = "class SalesOrder:\n    def on_submit(self):\n        frappe.get_pdf('x')\n"
    findings = SyncPdfGenerationInDocEventCheck().check_file("a.py", content, None)
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-025"


def test_blocking_call_titles_are_not_fuzzy_duplicates_of_each_other():
    # regression: near-identical boilerplate titles ("... inside a document
    # lifecycle hook") on two adjacent lines made the aggregator's
    # cross-check dedup heuristic (line-window + title-similarity) collapse
    # a real RULE-024 finding and a real RULE-025 finding into one, silently
    # dropping one of two genuinely distinct issues.
    from difflib import SequenceMatcher

    ratio = SequenceMatcher(
        None,
        BlockingHttpCallInDocEventCheck.title.lower(),
        SyncPdfGenerationInDocEventCheck.title.lower(),
    ).ratio()
    assert ratio < 0.6


# RULE-026: .save() inside a loop
def test_save_in_loop_flagged():
    content = "for d in docs:\n    d.save()\n"
    findings = SaveInLoopCheck().check_file("a.py", content, None)
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-026"


def test_save_outside_loop_not_flagged():
    content = "doc.save()\n"
    assert SaveInLoopCheck().check_file("a.py", content, None) == []


# RULE-027: frappe.call() promise chain could use async/await
def test_promise_chain_without_async_flagged():
    content = "frappe.call({method: 'x'}).then(function(r) { console.log(r); });\n"
    findings = JsAsyncAwaitSuggestionCheck().check_file("a.js", content, None)
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-027"


def test_async_function_not_flagged():
    content = "async function go() {\n  await frappe.call({method: 'x'});\n}\n"
    assert JsAsyncAwaitSuggestionCheck().check_file("a.js", content, None) == []


# RULE-028: direct DOM manipulation
def test_get_element_by_id_flagged():
    findings = JsDirectDomCheck().check_file("a.js", "document.getElementById('x');\n", None)
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-028"


def test_frm_api_not_flagged():
    assert JsDirectDomCheck().check_file("a.js", "frm.set_value('x', 1);\n", None) == []


# RULE-029: client script too long
def test_long_client_script_flagged():
    content = "\n".join(f"var x{i} = {i};" for i in range(250))
    findings = JsClientScriptLengthCheck().check_file("a.js", content, None)
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-029"


def test_short_client_script_not_flagged():
    content = "\n".join(f"var x{i} = {i};" for i in range(10))
    assert JsClientScriptLengthCheck().check_file("a.js", content, None) == []


def test_long_script_not_flagged_when_diff_touches_nothing():
    content = "\n".join(f"var x{i} = {i};" for i in range(250))
    assert JsClientScriptLengthCheck().check_file("a.js", content, set()) == []


# RULE-030/RULE-031: commented-out code
def test_commented_out_python_assignment_flagged():
    content = "x = 1\n# y = compute(x, 2)\n"
    findings = CommentedOutPythonCodeCheck().check_file("a.py", content, None)
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-030"
    assert findings[0].line_start == 2


def test_prose_comment_not_flagged_as_code():
    content = "# TODO: revisit this later\n"
    assert CommentedOutPythonCodeCheck().check_file("a.py", content, None) == []


def test_type_comment_not_flagged():
    content = "x = []  # type: list[int]\n"
    assert CommentedOutPythonCodeCheck().check_file("a.py", content, None) == []


def test_commented_out_js_call_flagged():
    content = "let x = compute();\n// doSomething(x);\n"
    findings = CommentedOutJsCodeCheck().check_file("a.js", content, None)
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-031"


def test_js_prose_comment_not_flagged():
    content = "// this handles the edge case below\n"
    assert CommentedOutJsCodeCheck().check_file("a.js", content, None) == []


# RULE-032: magic numbers
def test_magic_number_in_comparison_flagged():
    content = "if amount > 4837:\n    pass\n"
    findings = MagicNumberCheck().check_file("a.py", content, None)
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-032"


def test_exempt_numbers_not_flagged():
    content = "i = i + 1\ntotal = base * 100\nx = base * 2\n"
    assert MagicNumberCheck().check_file("a.py", content, None) == []


def test_constant_declaration_not_flagged():
    content = "MAX_RETRIES = 4837\n"
    assert MagicNumberCheck().check_file("a.py", content, None) == []


# RULE-033: missing docstring
def test_missing_docstring_flagged():
    content = "def process(order):\n    return order.total\n"
    findings = MissingDocstringCheck().check_file("a.py", content, None)
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-033"


def test_present_docstring_not_flagged():
    content = 'def process(order):\n    """Process an order."""\n    return order.total\n'
    assert MissingDocstringCheck().check_file("a.py", content, None) == []


def test_private_helper_not_flagged():
    content = "def _process(order):\n    return order.total\n"
    assert MissingDocstringCheck().check_file("a.py", content, None) == []


# RULE-034: boolean default parameter
def test_boolean_default_param_flagged():
    content = "def notify(user, send_email=True):\n    pass\n"
    findings = BooleanFlagParamCheck().check_file("a.py", content, None)
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-034"


def test_non_boolean_default_not_flagged():
    content = "def notify(user, retries=3):\n    pass\n"
    assert BooleanFlagParamCheck().check_file("a.py", content, None) == []


# RULE-035: committed .env file not covered by .gitignore
def _target(path: str) -> ReviewTarget:
    return ReviewTarget(path=path, status="scanned", changed_lines=None)


def test_env_file_without_gitignore_flagged(tmp_path):
    (tmp_path / ".env").write_text("SECRET=1\n")
    findings = SecretsInRepoRunner().run([_target(".env")], tmp_path)
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-035"
    assert findings[0].file == ".env"


def test_env_file_covered_by_gitignore_not_flagged(tmp_path):
    (tmp_path / ".env").write_text("SECRET=1\n")
    (tmp_path / ".gitignore").write_text(".env\n")
    assert SecretsInRepoRunner().run([_target(".env")], tmp_path) == []


def test_no_env_file_not_flagged(tmp_path):
    assert SecretsInRepoRunner().run([], tmp_path) == []


def test_nested_env_file_flagged_with_relative_path(tmp_path):
    nested = tmp_path / "config"
    nested.mkdir()
    (nested / ".env").write_text("SECRET=1\n")
    findings = SecretsInRepoRunner().run([], tmp_path)
    assert len(findings) == 1
    assert findings[0].file == "config/.env"

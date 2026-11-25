import ast

from codecheck.checks.engine import AstCheckEngine, AstContext, AstRule, Violation
from codecheck.models import Severity


class BareExceptRule(AstRule):
    check_id = "RULE-001"
    title = "Bare except clause"
    severity = Severity.MEDIUM
    node_types = (ast.ExceptHandler,)

    def match(self, node: ast.ExceptHandler, ctx: AstContext) -> Violation | None:
        if node.type is None:
            return Violation(node=node, explanation="Bare except")
        return None


def test_ast_check_engine_detects_violation():
    engine = AstCheckEngine([BareExceptRule()])
    code = "try:\n    pass\nexcept:\n    pass\n"
    findings = engine.check_file("test.py", code, changed_lines=None)
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-001"
    assert findings[0].line_start == 3


def test_ast_check_engine_filters_unchanged_lines():
    engine = AstCheckEngine([BareExceptRule()])
    code = "try:\n    pass\nexcept:\n    pass\n"
    findings = engine.check_file("test.py", code, changed_lines={1})
    assert len(findings) == 0


def test_default_ast_check_engine_parity():
    from codecheck.checks.leftover_print import LeftoverPrintCheck
    from codecheck.checks.missing_translation import MissingTranslationCheck
    from codecheck.checks.n_plus_one_query import NPlusOneQueryCheck
    from codecheck.checks.no_bare_except import NoBareExceptCheck
    from codecheck.checks.no_manual_commit import NoManualCommitCheck
    from codecheck.checks.no_sql_string_format import NoSqlStringFormatCheck
    from codecheck.checks.silent_exception import SilentExceptionCheck

    snippet = (
        "import frappe\n\n"
        "def bad_function():\n"
        "    print('debug')\n"
        "    frappe.db.commit()\n"
        "    frappe.throw('untranslated')\n"
        "    try:\n"
        "        pass\n"
        "    except:\n"
        "        pass\n"
        "    try:\n"
        "        pass\n"
        "    except ValueError:\n"
        "        pass\n"
        "    for x in [1, 2]:\n"
        "        frappe.get_doc('User', x)\n"
        "    frappe.db.sql(f'SELECT * FROM tabUser WHERE name = {x}')\n"
    )

    legacy_checks = [
        NoBareExceptCheck(),
        NoSqlStringFormatCheck(),
        NPlusOneQueryCheck(),
        NoManualCommitCheck(),
        MissingTranslationCheck(),
        LeftoverPrintCheck(),
        SilentExceptionCheck(),
    ]
    legacy_findings = []
    for c in legacy_checks:
        legacy_findings.extend(c.check_file("sample.py", snippet, changed_lines=None))

    engine = AstCheckEngine()
    engine_findings = engine.check_file("sample.py", snippet, changed_lines=None)

    legacy_set = {(f.check_id, f.line_start) for f in legacy_findings}
    engine_set = {(f.check_id, f.line_start) for f in engine_findings}

    assert legacy_set == engine_set
    assert len(legacy_set) == 7

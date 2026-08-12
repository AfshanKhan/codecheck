from codecheck.checks.no_bare_except import NoBareExceptCheck
from codecheck.checks.no_sql_string_format import NoSqlStringFormatCheck


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

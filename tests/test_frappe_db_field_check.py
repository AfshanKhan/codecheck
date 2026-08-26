from pathlib import Path
from unittest.mock import MagicMock

from codecheck.checks.frappe_db_field_check import (
    FrappeDbFieldCheckRunner,
    _extract_field_references,
    _is_plain_fieldname,
)
from codecheck.models import ReviewTarget


def test_is_plain_fieldname_accepts_bare_identifiers():
    assert _is_plain_fieldname("customer") is True
    assert _is_plain_fieldname("custom_notes") is True


def test_is_plain_fieldname_rejects_wildcard_and_empty():
    assert _is_plain_fieldname("*") is False
    assert _is_plain_fieldname("") is False


def test_is_plain_fieldname_rejects_child_table_qualified_name():
    assert _is_plain_fieldname("customer.customer_name") is False


def test_is_plain_fieldname_rejects_sql_function_call():
    assert _is_plain_fieldname("count(name)") is False


def test_is_plain_fieldname_rejects_alias():
    assert _is_plain_fieldname("name as party") is False


def test_extract_field_references_get_value_single_field():
    import ast

    tree = ast.parse("frappe.db.get_value('Sales Order', so_name, 'customer')")
    refs = _extract_field_references(tree)
    assert [(d, f) for d, f, _ in refs] == [("Sales Order", "customer")]


def test_extract_field_references_get_value_field_list():
    import ast

    tree = ast.parse(
        "frappe.db.get_value('Sales Order', so_name, ['customer', 'total'])"
    )
    refs = _extract_field_references(tree)
    assert [(d, f) for d, f, _ in refs] == [
        ("Sales Order", "customer"),
        ("Sales Order", "total"),
    ]


def test_extract_field_references_get_all_fields_kwarg():
    import ast

    tree = ast.parse(
        "frappe.get_all('Sales Order', filters={}, fields=['customer', 'total'])"
    )
    refs = _extract_field_references(tree)
    assert [(d, f) for d, f, _ in refs] == [
        ("Sales Order", "customer"),
        ("Sales Order", "total"),
    ]


def test_extract_field_references_skips_non_literal_doctype():
    import ast

    tree = ast.parse("frappe.db.get_value(doctype_var, name, 'customer')")
    assert _extract_field_references(tree) == []


def test_extract_field_references_skips_qualified_and_wildcard_fields():
    import ast

    tree = ast.parse(
        "frappe.get_all('Sales Order', fields=['customer.customer_name', '*', 'count(name)'])"
    )
    assert _extract_field_references(tree) == []


def test_runner_flags_missing_field(tmp_path):
    db = MagicMock()
    db.doctype_fields.return_value = frozenset({"total"})
    runner = FrappeDbFieldCheckRunner(db)

    repo = tmp_path
    (repo / "app.py").write_text(
        "frappe.db.get_value('Sales Order', so_name, 'customer')\n"
    )
    target = ReviewTarget(path="app.py", status="modified", changed_lines=None)

    findings = runner.run([target], repo)
    assert len(findings) == 1
    assert findings[0].check_id == "RULE-019"
    assert "customer" in findings[0].explanation
    assert "Sales Order" in findings[0].explanation


def test_runner_does_not_flag_existing_field(tmp_path):
    db = MagicMock()
    db.doctype_fields.return_value = frozenset({"customer", "total"})
    runner = FrappeDbFieldCheckRunner(db)

    repo = tmp_path
    (repo / "app.py").write_text(
        "frappe.db.get_value('Sales Order', so_name, 'customer')\n"
    )
    target = ReviewTarget(path="app.py", status="modified", changed_lines=None)

    assert runner.run([target], repo) == []


def test_runner_skips_unknown_doctype(tmp_path):
    db = MagicMock()
    db.doctype_fields.return_value = None
    runner = FrappeDbFieldCheckRunner(db)

    repo = tmp_path
    (repo / "app.py").write_text(
        "frappe.db.get_value('Not A Doctype', name, 'whatever')\n"
    )
    target = ReviewTarget(path="app.py", status="modified", changed_lines=None)

    assert runner.run([target], repo) == []


def test_runner_respects_changed_lines_scope(tmp_path):
    db = MagicMock()
    db.doctype_fields.return_value = frozenset({"total"})
    runner = FrappeDbFieldCheckRunner(db)

    repo = tmp_path
    (repo / "app.py").write_text(
        "x = 1\nfrappe.db.get_value('Sales Order', so_name, 'customer')\n"
    )
    # Line 2 has the bad reference but is out of diff scope.
    target = ReviewTarget(path="app.py", status="modified", changed_lines={1})

    assert runner.run([target], repo) == []


def test_runner_skips_deleted_files(tmp_path):
    db = MagicMock()
    runner = FrappeDbFieldCheckRunner(db)
    target = ReviewTarget(path="app.py", status="deleted", changed_lines=None)

    assert runner.run([target], tmp_path) == []
    db.doctype_fields.assert_not_called()


def test_runner_skips_non_python_files(tmp_path):
    db = MagicMock()
    runner = FrappeDbFieldCheckRunner(db)
    (tmp_path / "app.js").write_text("frappe.db.get_value('Sales Order', n, 'customer')\n")
    target = ReviewTarget(path="app.js", status="modified", changed_lines=None)

    assert runner.run([target], tmp_path) == []
    db.doctype_fields.assert_not_called()


def test_runner_is_available_always_true(tmp_path):
    runner = FrappeDbFieldCheckRunner(MagicMock())
    assert runner.is_available(Path(tmp_path)) == (True, None)

"""RULE-036: a Custom Field's name must start with `custom_`."""

import json

import pytest

from codecheck.checks.custom_field_prefix import CustomFieldPrefixCheck
from codecheck.checks.registry import ALL_CHECKS

check = CustomFieldPrefixCheck()


def run(path, content, changed=None):
    return check.check_file(path, content, changed)


def fixture(*fields):
    return json.dumps([{"doctype": "Custom Field", **f} for f in fields], indent=1)


def test_it_is_registered():
    assert any(c.check_id == "RULE-036" for c in ALL_CHECKS)


# --- fixtures -----------------------------------------------------------------------------------------

def test_fixture_field_without_prefix_is_flagged_with_its_doctype_and_line():
    content = fixture({"dt": "Employee", "fieldname": "custom_ok"}, {"dt": "Sales Order", "fieldname": "region_code"})
    (f,) = run("app/fixtures/custom_field.json", content)
    assert f.check_id == "RULE-036" and f.tier == "rules" and f.source == "house"
    assert "'region_code'" in f.explanation and "Sales Order" in f.explanation and "custom_region_code" in f.explanation
    assert "region_code" in content.splitlines()[f.line_start - 1]


def test_prefixed_fixture_field_and_field_without_a_name_are_fine():
    assert run("app/fixtures/custom_field.json", fixture({"dt": "Employee", "fieldname": "custom_ok"}, {"dt": "Employee", "label": "Derived"})) == []


def test_other_fixture_records_are_ignored():
    content = json.dumps([{"doctype": "Property Setter", "fieldname": "naming_series"}, {"doctype": "Custom Script", "fieldname": "x"}])
    assert run("app/fixtures/property_setter.json", content) == []


@pytest.mark.parametrize("path", ["app/data/custom_field.json", "app/doctype/x/x.json", "package.json", "fixtures_custom_field.json"])
def test_json_outside_fixtures_and_custom_folders_is_ignored(path):
    assert run(path, fixture({"dt": "Employee", "fieldname": "region"})) == []


def test_same_name_on_two_doctypes_gets_two_distinct_lines():
    content = fixture({"dt": "Employee", "fieldname": "region"}, {"dt": "Customer", "fieldname": "region"})
    findings = run("app/fixtures/custom_field.json", content)
    lines = [x.line_start for x in findings]
    assert len(lines) == 2 and lines[0] < lines[1]                      # the 1st and 2nd occurrence, not the same line twice
    assert "Employee" in findings[0].explanation and "Customer" in findings[1].explanation


# --- exported customizations (<module>/custom/<doctype>.json) ----------------------------------------

def test_exported_customization_custom_fields_are_checked_but_property_setters_are_not():
    data = {"doctype": "Employee", "custom_fields": [{"fieldname": "badge_no", "dt": "Employee"}, {"fieldname": "custom_ok"}],
            "property_setters": [{"field_name": "naming_series", "property": "hidden"}]}
    findings = run("app/app/custom/employee.json", json.dumps(data, indent=1))
    assert [("badge_no" in f.explanation) for f in findings] == [True]
    assert "Employee" in findings[0].explanation


def test_fields_that_name_another_apps_module_are_skipped():
    data = {"doctype": "Item", "custom_fields": [
        {"fieldname": "gst_hsn_code", "module": "GST India"}, {"fieldname": "business_units", "module": None}, {"fieldname": "region", "module": ""}]}
    names = [f.explanation.split("'")[1] for f in run("app/app/custom/item.json", json.dumps(data, indent=1))]
    assert names == ["business_units", "region"]
    fx = fixture({"dt": "Item", "fieldname": "gstin", "module": "GST India"}, {"dt": "Item", "fieldname": "region"})
    assert [f.explanation.split("'")[1] for f in run("app/fixtures/custom_field.json", fx)] == ["region"]


def test_invalid_or_unexpected_json_does_not_crash():
    assert run("app/fixtures/custom_field.json", "{ not json") == []
    assert run("app/fixtures/custom_field.json", json.dumps({"a": 1})) == []
    assert run("app/app/custom/employee.json", json.dumps([1, 2])) == []


# --- Python -------------------------------------------------------------------------------------------

def test_create_custom_fields_mapping_is_checked_with_the_doctype():
    content = (
        "from frappe.custom.doctype.custom_field.custom_field import create_custom_fields\n\n"
        "def execute():\n"
        "    create_custom_fields({\n"
        '        "Employee": [\n'
        '            {"fieldname": "custom_ok", "fieldtype": "Data"},\n'
        '            {"fieldname": "badge_no", "fieldtype": "Data"},\n'
        "        ],\n"
        '        ("Sales Order", "Quotation"): [{"fieldname": "region", "fieldtype": "Data"}],\n'
        "    })\n"
    )
    findings = run("app/patches/add_fields.py", content)
    assert [f.line_start for f in findings] == [7, 9]
    assert "'badge_no'" in findings[0].explanation and "on Employee" in findings[0].explanation
    assert "'region'" in findings[1].explanation


def test_module_level_constant_passed_to_create_custom_fields_is_resolved():
    content = (
        'FIELDS = {"Employee": [{"fieldname": "badge_no"}, {"fieldname": "custom_ok"}]}\n\n'
        "def execute():\n    create_custom_fields(FIELDS)\n"
    )
    (f,) = run("app/setup.py", content)
    assert "badge_no" in f.explanation and f.line_start == 1


def test_singular_create_custom_field_and_get_doc_dicts_are_checked():
    content = (
        "def execute():\n"
        '    create_custom_field("Employee", {"fieldname": "badge_no", "fieldtype": "Data"})\n'
        '    frappe.get_doc({"doctype": "Custom Field", "dt": "Customer", "fieldname": "region"}).insert()\n'
    )
    findings = run("app/patch.py", content)
    assert sorted(f.line_start for f in findings) == [2, 3]
    assert any("Customer" in f.explanation for f in findings)


def test_python_that_is_not_about_custom_fields_is_left_alone():
    content = (
        'def f():\n    x = {"fieldname": "region"}\n    return frappe.get_doc({"doctype": "ToDo", "fieldname": "region"})\n'
        "def create_custom_fields_report():\n    pass\n"
    )
    assert run("app/a.py", content) == []
    assert run("app/a.py", "def broken(:\n") == []
    assert run("app/a.js", 'create_custom_fields({"fieldname": "x"})') == []


def test_prefixed_python_fields_and_non_literal_names_are_fine():
    content = 'create_custom_fields({"Employee": [{"fieldname": "custom_a"}, {"fieldname": name}, {"fieldname": f"x_{y}"}]})\n'
    assert run("app/a.py", content) == []


# --- diff mode ----------------------------------------------------------------------------------------

def test_only_changed_lines_are_reported_in_diff_mode():
    content = fixture({"dt": "Employee", "fieldname": "region"})
    (f,) = run("app/fixtures/custom_field.json", content)
    assert run("app/fixtures/custom_field.json", content, {f.line_start}) != []
    assert run("app/fixtures/custom_field.json", content, {f.line_start + 50}) == []
    py = 'create_custom_fields({"Employee": [{"fieldname": "badge_no"}]})\n'
    assert run("a.py", py, {1}) != [] and run("a.py", py, {2}) == []


# --- review follow-ups --------------------------------------------------------------------------------

def test_a_skipped_field_with_the_same_name_does_not_shift_the_line_of_the_reported_one():
    data = {"doctype": "Item", "custom_fields": [
        {"fieldname": "region", "module": "GST India", "dt": "Item"}, {"fieldname": "region", "dt": "Customer"}]}
    content = json.dumps(data, indent=1)
    (f,) = run("app/app/custom/item.json", content)
    lines = content.splitlines()
    assert "region" in lines[f.line_start - 1] and f.line_start == [i + 1 for i, x in enumerate(lines) if "region" in x][1]
    assert "Customer" in f.explanation
    assert run("app/app/custom/item.json", content, {f.line_start}) != []          # touching only the real field still warns
    assert run("app/app/custom/item.json", content, {f.line_start - 7}) == []


def test_other_records_with_a_fieldname_in_a_fixture_do_not_shift_the_line_either():
    content = json.dumps([
        {"doctype": "Property Setter", "fieldname": "region"},
        {"doctype": "Custom Field", "dt": "Item", "fieldname": "region", "module": "GST India"},
        {"doctype": "Custom Field", "dt": "Customer", "fieldname": "region"}], indent=1)
    (f,) = run("app/fixtures/custom_field.json", content)
    assert "Customer" in f.explanation
    occurrences = [i + 1 for i, x in enumerate(content.splitlines()) if '"fieldname": "region"' in x]
    assert f.line_start == occurrences[2]                                                                 # the third, not the first


def test_a_parameter_or_local_with_the_constants_name_hides_the_module_constant():
    base = 'FIELDS = {"Employee": [{"fieldname": "region"}]}\n\n'
    assert len(run("a.py", base + "def run():\n    create_custom_fields(FIELDS)\n")) == 1                  # really the module's
    assert run("a.py", base + "def run(FIELDS):\n    create_custom_fields(FIELDS)\n") == []                # the caller's own
    assert run("a.py", base + 'def run():\n    FIELDS = {"Employee": [{"fieldname": "custom_a"}]}\n    create_custom_fields(FIELDS)\n') == []
    assert run("a.py", base + "def run(*FIELDS):\n    create_custom_fields(FIELDS)\n") == []
    assert len(run("a.py", base + "def other(FIELDS):\n    pass\n\ndef run():\n    create_custom_fields(FIELDS)\n")) == 1   # another function's parameter


def test_the_doctype_is_named_for_the_singular_call_and_for_tuple_keys():
    (f,) = run("a.py", 'create_custom_field("Employee", {"fieldname": "badge_no"})\n')
    assert "on Employee" in f.explanation
    (f,) = run("a.py", 'create_custom_field(doctype="Customer", df={"fieldname": "region"})\n')
    assert "on Customer" in f.explanation
    (f,) = run("a.py", 'create_custom_fields({("Sales Order", "Quotation"): [{"fieldname": "region"}]})\n')
    assert "on Sales Order, Quotation" in f.explanation
    (f,) = run("a.py", "create_custom_field(some_doctype, {\"fieldname\": \"region\"})\n")
    assert "'region' should be named" in f.explanation                                                   # unknown stays unnamed

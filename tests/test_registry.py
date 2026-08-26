from codecheck.checks.no_bare_except import NoBareExceptCheck
from codecheck.checks.registry import ALL_CHECKS, load_extra_checks


def test_load_extra_checks_imports_and_instantiates_a_real_check():
    checks, errors = load_extra_checks(["codecheck.checks.no_bare_except:NoBareExceptCheck"])
    assert errors == []
    assert len(checks) == 1
    assert isinstance(checks[0], NoBareExceptCheck)


def test_load_extra_checks_empty_list_is_a_no_op():
    checks, errors = load_extra_checks([])
    assert checks == []
    assert errors == []


def test_load_extra_checks_rejects_malformed_path():
    checks, errors = load_extra_checks(["not_a_dotted_path"])
    assert checks == []
    assert len(errors) == 1
    assert "expected" in errors[0]


def test_load_extra_checks_reports_import_error_not_crash():
    checks, errors = load_extra_checks(["nonexistent_package.module:SomeCheck"])
    assert checks == []
    assert len(errors) == 1
    assert "nonexistent_package.module:SomeCheck" in errors[0]


def test_load_extra_checks_reports_missing_class_not_crash():
    checks, errors = load_extra_checks(["codecheck.checks.no_bare_except:NoSuchClass"])
    assert checks == []
    assert len(errors) == 1


def test_load_extra_checks_rejects_non_housecheck_class():
    # a real, importable class that just isn't a HouseCheck subclass
    checks, errors = load_extra_checks(["codecheck.models:Finding"])
    assert checks == []
    assert len(errors) == 1
    assert "not a HouseCheck subclass" in errors[0]


def test_load_extra_checks_reports_instantiation_failure_not_crash():
    # HouseCheck itself is abstract -- issubclass(HouseCheck, HouseCheck) is
    # trivially True, but instantiating it directly raises TypeError.
    checks, errors = load_extra_checks(["codecheck.checks.base:HouseCheck"])
    assert checks == []
    assert len(errors) == 1
    assert "failed to instantiate" in errors[0]


def test_load_extra_checks_one_bad_entry_does_not_block_a_good_one():
    checks, errors = load_extra_checks(
        ["nonexistent_package.module:SomeCheck", "codecheck.checks.no_bare_except:NoBareExceptCheck"]
    )
    assert len(checks) == 1
    assert len(errors) == 1


def test_all_checks_registry_is_unaffected_by_extra_checks_loading():
    # calling load_extra_checks must never mutate the module-level built-in list
    baseline = list(ALL_CHECKS)
    load_extra_checks(["codecheck.checks.no_bare_except:NoBareExceptCheck"])
    assert ALL_CHECKS == baseline

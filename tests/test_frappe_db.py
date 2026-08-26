import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codecheck.frappe_db import META_FIELDS, FrappeDbConnection, FrappeDbUnavailable


def _write_config(tmp_path: Path, **overrides) -> Path:
    config = {"db_name": "site_db", "db_password": "secret", **overrides}
    path = tmp_path / "site_config.json"
    path.write_text(json.dumps(config))
    return path


def test_connect_raises_when_file_missing(tmp_path):
    with pytest.raises(FrappeDbUnavailable, match="could not read"):
        FrappeDbConnection.connect(tmp_path / "nope.json")


def test_connect_raises_on_invalid_json(tmp_path):
    path = tmp_path / "site_config.json"
    path.write_text("not json")
    with pytest.raises(FrappeDbUnavailable, match="not valid JSON"):
        FrappeDbConnection.connect(path)


def test_connect_raises_when_config_is_not_an_object(tmp_path):
    path = tmp_path / "site_config.json"
    path.write_text("[1, 2, 3]")
    with pytest.raises(FrappeDbUnavailable, match="does not contain a JSON object"):
        FrappeDbConnection.connect(path)


def test_connect_raises_on_unsupported_db_type(tmp_path):
    path = _write_config(tmp_path, db_type="postgres")
    with pytest.raises(FrappeDbUnavailable, match="only supports db_type: mariadb"):
        FrappeDbConnection.connect(path)


def test_connect_raises_when_missing_credentials(tmp_path):
    path = tmp_path / "site_config.json"
    path.write_text(json.dumps({"db_name": "site_db"}))
    with pytest.raises(FrappeDbUnavailable, match="missing db_name/db_password"):
        FrappeDbConnection.connect(path)


def test_connect_raises_when_pymysql_not_installed(tmp_path, monkeypatch):
    path = _write_config(tmp_path)
    import codecheck.frappe_db as frappe_db_module

    def _raise_import_error():
        raise FrappeDbUnavailable("--frappe-db-config needs the 'pymysql' package")

    monkeypatch.setattr(frappe_db_module, "_import_pymysql", _raise_import_error)
    with pytest.raises(FrappeDbUnavailable, match="pymysql"):
        FrappeDbConnection.connect(path)


def test_doctype_fields_returns_none_for_unknown_doctype():
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    cursor.fetchone.return_value = None
    connection = MagicMock()
    connection.cursor.return_value = cursor

    db = FrappeDbConnection(connection)
    assert db.doctype_fields("Nonexistent DocType") is None
    # Second call should hit the cache, not re-query.
    assert db.doctype_fields("Nonexistent DocType") is None
    assert cursor.execute.call_count == 1


def test_doctype_fields_merges_standard_custom_and_meta_fields():
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    cursor.fetchone.return_value = {"name": "Sales Order"}
    cursor.fetchall.side_effect = [
        [{"fieldname": "customer"}, {"fieldname": "total"}],
        [{"fieldname": "custom_notes"}],
    ]
    connection = MagicMock()
    connection.cursor.return_value = cursor

    db = FrappeDbConnection(connection)
    fields = db.doctype_fields("Sales Order")

    assert fields is not None
    assert "customer" in fields
    assert "total" in fields
    assert "custom_notes" in fields
    assert META_FIELDS <= fields


def test_doctype_fields_caches_result_across_calls():
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    cursor.fetchone.return_value = {"name": "Sales Order"}
    cursor.fetchall.side_effect = [
        [{"fieldname": "customer"}],
        [],
        [{"fieldname": "customer"}],
        [],
    ]
    connection = MagicMock()
    connection.cursor.return_value = cursor

    db = FrappeDbConnection(connection)
    first = db.doctype_fields("Sales Order")
    second = db.doctype_fields("Sales Order")
    assert first == second
    assert cursor.execute.call_count == 3  # existence check + 2 field queries, once only


def test_close_delegates_to_connection():
    connection = MagicMock()
    db = FrappeDbConnection(connection)
    db.close()
    connection.close.assert_called_once()

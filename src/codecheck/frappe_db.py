"""Read-only connection to a live Frappe site's database, for the opt-in
`--frappe-db-config` checks. Reads connection details from a real
site_config.json already on disk.

Every query is a hardcoded, parameterized SELECT against Frappe's own
schema/metadata tables; no path for user-supplied SQL, and it never writes.
"""

from __future__ import annotations

import json
from pathlib import Path


class FrappeDbUnavailable(Exception):
    """Raised when a config/connection problem means the DB-backed checks
    can't run this session -- treated as a graceful skip, not an abort."""


# Fields Frappe attaches to every DocType automatically -- never flagged as
# "doesn't exist."
META_FIELDS = frozenset(
    {
        "name",
        "owner",
        "creation",
        "modified",
        "modified_by",
        "docstatus",
        "idx",
        "_comments",
        "_assign",
        "_liked_by",
        "_user_tags",
        "parent",
        "parentfield",
        "parenttype",
    }
)


def _import_pymysql():
    try:
        import pymysql
    except ImportError as e:
        raise FrappeDbUnavailable(
            "--frappe-db-config needs the 'pymysql' package -- install it with "
            "`uv sync --extra frappe-db` (dev mode) or `pip install codecheck[frappe-db]`"
        ) from e
    return pymysql


class FrappeDbConnection:
    def __init__(self, connection):
        self._connection = connection
        self._doctype_fields_cache: dict[str, frozenset[str] | None] = {}

    @classmethod
    def connect(cls, site_config_path: Path) -> "FrappeDbConnection":
        try:
            config = json.loads(site_config_path.read_text())
        except OSError as e:
            raise FrappeDbUnavailable(f"could not read {site_config_path}: {e}") from e
        except json.JSONDecodeError as e:
            raise FrappeDbUnavailable(f"{site_config_path} is not valid JSON: {e}") from e
        if not isinstance(config, dict):
            raise FrappeDbUnavailable(f"{site_config_path} does not contain a JSON object")

        db_type = config.get("db_type", "mariadb")
        if db_type != "mariadb":
            raise FrappeDbUnavailable(
                f"--frappe-db-config only supports db_type: mariadb right now, "
                f"got {db_type!r} from {site_config_path}"
            )
        db_name = config.get("db_name")
        db_password = config.get("db_password")
        if not db_name or not db_password:
            raise FrappeDbUnavailable(f"{site_config_path} is missing db_name/db_password")

        pymysql = _import_pymysql()
        try:
            connection = pymysql.connect(
                host=config.get("db_host") or "127.0.0.1",
                port=int(config.get("db_port") or 3306),
                # Frappe's own convention: the DB username is always db_name itself.
                user=db_name,
                password=db_password,
                database=db_name,
                connect_timeout=5,
                cursorclass=pymysql.cursors.DictCursor,
            )
        except pymysql.MySQLError as e:
            raise FrappeDbUnavailable(f"could not connect to the site's database: {e}") from e
        return cls(connection)

    def close(self) -> None:
        self._connection.close()

    def doctype_fields(self, doctype: str) -> frozenset[str] | None:
        """Every real field name on `doctype`: standard + custom fields +
        META_FIELDS. Returns None if `doctype` itself doesn't exist."""
        if doctype in self._doctype_fields_cache:
            return self._doctype_fields_cache[doctype]

        with self._connection.cursor() as cursor:
            cursor.execute("SELECT name FROM `tabDocType` WHERE name = %s", (doctype,))
            if cursor.fetchone() is None:
                self._doctype_fields_cache[doctype] = None
                return None

            cursor.execute("SELECT fieldname FROM `tabDocField` WHERE parent = %s", (doctype,))
            fields = {row["fieldname"] for row in cursor.fetchall() if row["fieldname"]}
            cursor.execute("SELECT fieldname FROM `tabCustom Field` WHERE dt = %s", (doctype,))
            fields |= {row["fieldname"] for row in cursor.fetchall() if row["fieldname"]}

        fields |= META_FIELDS
        result = frozenset(fields)
        self._doctype_fields_cache[doctype] = result
        return result

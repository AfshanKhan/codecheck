"""Fetches Server Script / Client Script content from a live Frappe site --
via the REST API, for sites with no local DB access -- and writes it to a
temp directory as ReviewTargets so the normal rules engine can run over it.
The DB path (FrappeDbConnection.fetch_scripts) returns the same shape.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from codecheck.models import ReviewTarget

# (subdirectory, file extension) per doctype -- Server Script is Python,
# Client Script is JS, so each gets routed to the right house/ruff/eslint checks.
_SCRIPT_LAYOUT = {
    "Server Script": ("server_script", "py"),
    "Client Script": ("client_script", "js"),
}

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


class FrappeScriptFetchError(Exception):
    """Raised for anything wrong with the REST API request/response that
    isn't already an httpx.HTTPError -- an insecure URL, or a response
    that doesn't look like Frappe's own list-of-records shape. Kept
    distinct from "zero scripts found" so a malformed payload can't be
    mistaken for a genuinely empty site."""


def fetch_via_api(site_url: str, api_key: str, api_secret: str) -> list[tuple[str, str, str]]:
    """(doctype, name, script) triples fetched from a live site's REST API."""
    parts = urlsplit(site_url)
    if parts.scheme != "https" and parts.hostname not in _LOOPBACK_HOSTS:
        raise FrappeScriptFetchError(
            f"refusing to send API credentials to {site_url!r} over plain HTTP -- "
            "use https://, or http://localhost or http://127.0.0.1 for local development"
        )

    headers = {"Authorization": f"token {api_key}:{api_secret}"}
    results: list[tuple[str, str, str]] = []
    with httpx.Client(base_url=site_url.rstrip("/"), headers=headers, timeout=30.0) as client:
        for doctype in _SCRIPT_LAYOUT:
            response = client.get(
                f"/api/resource/{doctype}",
                params={"fields": '["name", "script"]', "limit_page_length": "0"},
            )
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as e:  # json.JSONDecodeError is a ValueError subclass
                raise FrappeScriptFetchError(f"{doctype}: response wasn't valid JSON: {e}") from e
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise FrappeScriptFetchError(
                    f"{doctype}: unexpected response shape (expected a JSON object with a 'data' list)"
                )
            for row in payload["data"]:
                if not isinstance(row, dict):
                    continue
                name, script = row.get("name"), row.get("script")
                if name and script:
                    results.append((doctype, name, script))
    return results


_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9_-]+")


def write_scripts(scripts: list[tuple[str, str, str]], dest_dir: Path) -> list[ReviewTarget]:
    """Writes each (doctype, name, script) to dest_dir/<subdir>/<name>.<ext>.
    Returns matching ReviewTargets, changed_lines=None (whole-script scope,
    same as a whole-repo audit). Distinct record names that sanitize to the
    same filename (e.g. "a/b" and "a_b") get a -2, -3, ... suffix instead of
    silently overwriting each other."""
    targets = []
    used_paths: set[str] = set()
    for doctype, name, script in scripts:
        subdir, ext = _SCRIPT_LAYOUT[doctype]
        safe_name = _UNSAFE_FILENAME_RE.sub("_", name).strip("_") or "unnamed"
        rel_path = f"{subdir}/{safe_name}.{ext}"
        suffix = 2
        while rel_path in used_paths:
            rel_path = f"{subdir}/{safe_name}-{suffix}.{ext}"
            suffix += 1
        used_paths.add(rel_path)

        full_path = dest_dir / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(script, encoding="utf-8")
        targets.append(ReviewTarget(path=rel_path, status="scanned", changed_lines=None))
    return targets

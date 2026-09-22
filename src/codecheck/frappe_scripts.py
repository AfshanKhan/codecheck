"""Fetches Server Script / Client Script content from a live Frappe site --
via the REST API, for sites with no local DB access -- and writes it to a
temp directory as ReviewTargets so the normal rules engine can run over it.
The DB path (FrappeDbConnection.fetch_scripts) returns the same shape.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx

from codecheck.models import ReviewTarget

# (subdirectory, file extension) per doctype -- Server Script is Python,
# Client Script is JS, so each gets routed to the right house/ruff/eslint checks.
_SCRIPT_LAYOUT = {
    "Server Script": ("server_script", "py"),
    "Client Script": ("client_script", "js"),
}


def fetch_via_api(site_url: str, api_key: str, api_secret: str) -> list[tuple[str, str, str]]:
    """(doctype, name, script) triples fetched from a live site's REST API."""
    headers = {"Authorization": f"token {api_key}:{api_secret}"}
    results: list[tuple[str, str, str]] = []
    with httpx.Client(base_url=site_url.rstrip("/"), headers=headers, timeout=30.0) as client:
        for doctype in _SCRIPT_LAYOUT:
            response = client.get(
                f"/api/resource/{doctype}",
                params={"fields": '["name", "script"]', "limit_page_length": "0"},
            )
            response.raise_for_status()
            for row in response.json().get("data", []):
                script = row.get("script")
                if script:
                    results.append((doctype, row["name"], script))
    return results


_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9_-]+")


def write_scripts(scripts: list[tuple[str, str, str]], dest_dir: Path) -> list[ReviewTarget]:
    """Writes each (doctype, name, script) to dest_dir/<subdir>/<name>.<ext>.
    Returns matching ReviewTargets, changed_lines=None (whole-script scope,
    same as a whole-repo audit)."""
    targets = []
    for doctype, name, script in scripts:
        subdir, ext = _SCRIPT_LAYOUT[doctype]
        safe_name = _UNSAFE_FILENAME_RE.sub("_", name).strip("_") or "unnamed"
        rel_path = f"{subdir}/{safe_name}.{ext}"
        full_path = dest_dir / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(script, encoding="utf-8")
        targets.append(ReviewTarget(path=rel_path, status="scanned", changed_lines=None))
    return targets

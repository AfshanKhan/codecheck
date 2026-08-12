"""Render docs/*.md into wiki pages, rewriting links so they resolve on the
GitHub Wiki (a separate repo from the code):

- links to repo files one level up (`../src/...`, `../LICENSE`, `../README.md`,
  `../SECURITY.md`) become absolute GitHub "blob" URLs, since those files don't
  exist on the wiki;
- links between docs pages (`Page.md`, `Page.md#anchor`) become bare wiki page
  references (`Page`, `Page#anchor`), which is how the wiki links pages.

Run from the repo root with the wiki checked out at ./wiki and REPO=owner/name.
"""
import os
import pathlib
import re

repo = os.environ.get("REPO", "OWNER/REPO")
blob = f"https://github.com/{repo}/blob/main"

src = pathlib.Path("docs")
dst = pathlib.Path("wiki")

# Clear previously generated pages (never touch the wiki's .git).
for page in dst.glob("*.md"):
    page.unlink()


def rewrite(text: str) -> str:
    # 1) ../<repo-path> -> absolute blob URL (runs first so step 2 can't mangle it)
    text = re.sub(r"\]\(\.\./([^)]+)\)", lambda m: f"]({blob}/{m.group(1)})", text)
    # 2) internal doc link Page.md[#anchor] -> Page[#anchor]
    text = re.sub(
        r"\]\(([A-Za-z0-9._-]+)\.md(#[^)]*)?\)",
        lambda m: f"]({m.group(1)}{m.group(2) or ''})",
        text,
    )
    return text


count = 0
for page in src.glob("*.md"):
    (dst / page.name).write_text(rewrite(page.read_text()))
    count += 1
print(f"rendered {count} wiki pages from docs/")

from pathlib import Path

import pytest

from codecheck.models import ReviewTarget
from codecheck.noise import by_content, by_path, partition, summary


@pytest.mark.parametrize("path,why", [
    ("tests/test_x.py", "test"), ("app/test_orders.py", "test"), ("app/orders_test.py", "test"), ("tests.py", "test"),
    ("conftest.py", "test"), ("web/src/a.test.ts", "test"), ("web/a.spec.jsx", "test"), ("src/__tests__/a.js", "test"),
    ("cypress/e2e/a.cy.js", "test"), ("src/FooTest.java", "test"),
    ("public/dist/app.js", "bundle"), ("a/b/app.min.js", "bundle"), ("x.chunk.js", "bundle"),
    ("node_modules/x/index.js", "bundle"), ("vendor/lib.php", "bundle"), ("app.js.map", "bundle"), ("web/build/main.js", "bundle"),
    ("api/service_pb2.py", "generated"),
    ("app/orders.py", None), ("app/hooks.py", None), ("app/latest_report.py", None), ("src/contest.py", None),
    ("app/build/make.py", None), ("app/distribution.py", None),
])
def test_by_path(path, why):
    assert by_path(path) == why


@pytest.mark.parametrize("path,src,why", [
    ("a/__init__.py", "", "empty"), ("a/__init__.py", "\n\n", "empty"), ("a/__init__.py", "# just a comment\n", "empty"),
    ("a/__init__.py", '"""Package."""\n', "boilerplate"),
    ("a/__init__.py", '__version__ = "0.0.1"\n', "boilerplate"),
    ("a/__init__.py", "from .x import y\nfrom . import z\n__all__ = ['y']\n", "boilerplate"),
    ("a/doc.py", "import frappe\nfrom frappe.model.document import Document\n\nclass Order(Document):\n\tpass\n", "boilerplate"),
    ("a/doc.py", "import frappe\nfrom frappe.model.document import Document\n\n\nclass Order(Document):\n\t# begin: auto-generated types\n\t# This code is auto-generated. Do not modify anything in this block.\n"
     "\n\tfrom typing import TYPE_CHECKING\n\n\tif TYPE_CHECKING:\n\t\tfrom frappe.types import DF\n\n\t\tamount: DF.Currency\n\t# end: auto-generated types\n\n\tpass\n", "boilerplate"),
    ("a/doc.py", "# Copyright (c) 2026, X\n# For license information, please see license.txt\n\nimport frappe\nfrom frappe.model.document import Document\n\n\nclass Order(Document):\n\t# begin: auto-generated types\n\t# This code is auto-generated. Do not modify.\n\t# end: auto-generated types\n\n\tdef validate(self):\n\t\tself.total = 1\n", None),
    ("a/hooks.py", 'app_name = "x"\napp_title = "X"\n', None),
    ("a/doc.py", "class Order:\n    def validate(self):\n        pass\n", None),
    ("a/__init__.py", "try:\n    import x\nexcept ImportError:\n    x = None\n", None),
    ("a/util.py", "def f():\n    return 1\n", None),
    ("a/py2.py", "print 'hello'\n", None),
    ("web/index.js", "export * from './a';\nexport { b } from './b';\nimport './c';\n", "boilerplate"),
    ("web/a.js", "// nothing here\n/* license */\n", "empty"),
    ("web/form.js", "// Copyright (c) 2026\n\nfrappe.ui.form.on('Order', {\n\trefresh(frm) {\n\t\t// write your code here\n\t}\n});\n", "boilerplate"),
    ("web/form.js", "frappe.ui.form.on('Order', {\n\trefresh(frm) {\n\t\tfrm.add_custom_button('x', () => {});\n\t}\n});\n", None),
    ("web/a.js", "export function add(a, b) { return a + b }\n", None),
    ("web/min.js", "var a=" + "1+" * 900 + "1;\n", "bundle"),
    ("web/app.bundle.js", "(()=>{var __webpack_modules__={};function __webpack_require__(i){}\n})();\n", "bundle"),
    ("web/app.bundle.js", "import Grid from './grid';\n\nclass A extends Grid {\n\tvalidate() {\n\t\treturn 1;\n\t}\n}\n", None),   # Frappe source entry point
    ("a/gen.py", "# This file is auto-generated. DO NOT EDIT.\nX = 1\n", "generated"),
])
def test_by_content(path, src, why):
    assert by_content(path, src) == why


def test_partition_counts_and_keeps_real_code(tmp_path: Path):
    files = {"app/hooks.py": 'app_name = "x"\n', "app/__init__.py": "", "app/tests/test_a.py": "def test_a(): pass\n",
             "public/dist/a.js": "x", "app/doc.py": "class A:\n    pass\n", "app/logic.py": "def f():\n    return 1\n"}
    for name, src in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(src)
    targets = [ReviewTarget(path=n, status="scanned", diff_text="", changed_lines=None) for n in files]
    kept, left = partition(targets, tmp_path)
    assert [t.path for t in kept] == ["app/hooks.py", "app/logic.py"]
    assert dict(left) == {"empty": 1, "test": 1, "bundle": 1, "boilerplate": 1}
    assert summary(left).startswith("4 file(s) not sent to the AI (")


def test_run_llm_tier_leaves_noise_out_and_says_so_only_when_enabled(tmp_path: Path):
    from types import SimpleNamespace

    from codecheck.cli import _run_llm_tier
    (tmp_path / "app").mkdir()
    (tmp_path / "app/logic.py").write_text("def f():\n    return 1\n")
    (tmp_path / "app/__init__.py").write_text("")
    (tmp_path / "app/test_a.py").write_text("def test_a():\n    assert 1\n")
    targets = [ReviewTarget(path=p, status="scanned", diff_text="", changed_lines=None) for p in ("app/logic.py", "app/__init__.py", "app/test_a.py")]

    class Rev:
        skipped_files: list = []

        def __init__(self, skip_noise):
            self.config = SimpleNamespace(skip_noise=skip_noise)

        def review(self, ts, repo_path, on_progress=None):
            self.got = [t.path for t in ts]
            return []
    on, off = Rev(True), Rev(False)
    _, entries, _ = _run_llm_tier("cloud_llm", on, targets, tmp_path, None, "audit", {}, "s")
    assert on.got == ["app/logic.py"]
    assert len(entries) == 1 and entries[0].startswith("cloud_llm: 2 file(s) not sent to the AI (") and "tests" in entries[0] and "empty" in entries[0]
    _, entries, _ = _run_llm_tier("cloud_llm", off, targets, tmp_path, None, "audit", {}, "s")
    assert off.got == ["app/logic.py", "app/__init__.py", "app/test_a.py"] and entries == []

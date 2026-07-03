"""Guard against silently-shadowed method definitions (audit 2026-07-02).

``AlpacaClient`` once defined ``account_activities`` twice; Python keeps only the second
``def``, so the first — the one assignment detection called — silently vanished and every
daily OPASN check failed into a broad ``except`` for weeks. That class of bug is invisible
to the test suite (fakes implement whichever signature the test expects) but trivial to
catch statically: no class in the codebase may define the same method name twice.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("engine", "scripts", "dashboard")


def _py_files():
    for d in SCAN_DIRS:
        yield from sorted((ROOT / d).rglob("*.py"))


@pytest.mark.parametrize("path", list(_py_files()), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_class_defines_a_method_twice(path):
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        seen: dict[str, int] = {}
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # property setters/getters legitimately reuse the name; skip decorated redefs
                # that reference the same name (e.g. @x.setter).
                deco = {getattr(d, "attr", getattr(d, "id", None)) for d in item.decorator_list}
                if {"setter", "getter", "deleter"} & deco:
                    continue
                if item.name in seen:
                    pytest.fail(
                        f"{path.relative_to(ROOT)}: class {node.name} defines "
                        f"{item.name!r} twice (lines {seen[item.name]} and {item.lineno}) — "
                        f"the second silently shadows the first")
                seen[item.name] = item.lineno


from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
BIN = Path(__file__).resolve().parent.parent / "bin"

ENTRY = BIN / "claude-hub"
FOLDER = BIN.parent / "lib" / "claude-hub"


PART_STATEMENTS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign, ast.Import,
                   ast.ImportFrom, ast.Expr)


def stored_names(targets) -> list[str]:
    return [n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)]


def bindings(body) -> tuple[list[str], dict[str, str]]:
    defined: list[str] = []
    imported: dict[str, str] = {}
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.append(node.name)
        elif isinstance(node, ast.Assign):
            defined += stored_names(node.targets)
        elif isinstance(node, ast.AnnAssign):
            defined += stored_names([node.target])
        elif isinstance(node, ast.Import):
            for a in node.names:
                imported[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                imported[a.asname or a.name] = f"{'.' * node.level}{node.module or ''}:{a.name}"
    return defined, imported


def entry_parts() -> list[str]:
    tree = ast.parse(ENTRY.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and [t.id for t in node.targets if isinstance(t, ast.Name)] == ["PARTS"]:
            return list(ast.literal_eval(node.value))
    raise AssertionError("the entry has no PARTS tuple")


class HubPartsTests(unittest.TestCase):
    def test_parts_is_exactly_the_py_files_of_the_folder(self):
        parts = entry_parts()
        files = sorted(p.name for p in FOLDER.glob("*.py"))
        self.assertEqual(len(parts), len(set(parts)), "a part listed twice")
        self.assertEqual(sorted(parts), files, "PARTS and lib/claude-hub/*.py differ (an orphan or a missing part)")
        self.assertEqual(parts, sorted(parts), "PARTS runs in the order the folder lists")

    def test_every_part_starts_with_a_comment_header_and_the_future_import_and_never_a_string(self):
        for name in entry_parts():
            text = (FOLDER / name).read_text()
            tree = ast.parse(text)
            first = tree.body[0]
            self.assertIsInstance(first, ast.ImportFrom, f"{name}: the first statement is not an import")
            self.assertEqual((first.module, [a.name for a in first.names]), ("__future__", ["annotations"]), name)
            self.assertFalse(isinstance(first, ast.Expr), f"{name}: starts with a string, which would replace __doc__")
            self.assertTrue(text.startswith("# claude-hub, part "), f"{name}: no # header")
            lines = text.splitlines()
            before = lines[:first.lineno - 1]
            self.assertTrue(all(l.startswith("#") or not l.strip() for l in before), f"{name}: code before the future import")

    def test_no_top_level_name_is_defined_twice_across_the_parts_and_the_entry(self):
        seen: dict[str, list[str]] = {}
        imports: dict[str, dict[str, list[str]]] = {}
        for path in [ENTRY] + [FOLDER / n for n in entry_parts()]:
            body = ast.parse(path.read_text()).body
            if path != ENTRY:
                odd = [type(n).__name__ for n in body if not isinstance(n, PART_STATEMENTS)]
                self.assertEqual(odd, [], f"{path.name}: a top-level statement the duplicate-name checks cannot see into")
            defined, imported = bindings(body)
            for name in defined:
                seen.setdefault(name, []).append(path.name)
            for name, what in imported.items():
                imports.setdefault(name, {}).setdefault(what, []).append(path.name)
        dups = {k: v for k, v in seen.items() if len(v) > 1}
        self.assertEqual(dups, {}, "a name defined in two places: the later one silently replaces the earlier")
        clash = {k: v for k, v in imports.items() if k in seen or len(v) > 1}
        self.assertEqual(clash, {}, "a name both imported and defined, or imported as two different things")

    def test_no_method_is_defined_in_two_of_the_classes_popup_is_built_from(self):
        classes: dict[str, ast.ClassDef] = {}
        for name in entry_parts():
            for node in ast.parse((FOLDER / name).read_text()).body:
                if isinstance(node, ast.ClassDef):
                    classes[node.name] = node
        popup = classes["Popup"]
        self.assertTrue(all(isinstance(b, ast.Name) for b in popup.bases), "a base this check cannot follow")
        built_from = [popup] + [classes[b.id] for b in popup.bases]
        self.assertGreater(len(built_from), 1, "Popup has no mixins to check")
        seen: dict[str, list[str]] = {}
        for cls in built_from:
            for name in bindings(cls.body)[0]:
                seen.setdefault(name, []).append(cls.name)
        dups = {k: v for k, v in seen.items() if len(v) > 1}
        self.assertEqual(dups, {}, "a method or class attribute defined twice in the classes Popup is built from: "
                                   "the first in the MRO wins")

    def test_the_entry_keeps_the_docstring_main_prints(self):
        self.assertTrue(re.match(r'#!/usr/bin/env python3\n"""claude-hub', ENTRY.read_text()))
        self.assertTrue(ast.get_docstring(ast.parse(ENTRY.read_text())).startswith("claude-hub"))


if __name__ == "__main__":
    unittest.main()

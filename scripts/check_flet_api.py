#!/usr/bin/env python3
"""Static guard: every ft.* control call uses kwargs that exist in flet 0.86.5.

Resolves **kwargs splats by name, which a naive AST check misses -- that gap
let a `focused_border=` typo through and only surfaced at runtime in the
browser, where the whole app renders as "Error running app".
"""
import ast
import dataclasses
import pathlib
import sys

import flet as ft

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "telegram_downloader"


def literal_keys(fn, name):
    """Literal string keys of a local `name = dict(...)` or `name = {...}`."""
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)):
            continue
        if node.targets[0].id != name:
            continue
        val = node.value
        if isinstance(val, ast.Dict):
            keys = val.keys
        elif (isinstance(val, ast.Call) and isinstance(val.func, ast.Name)
              and val.func.id == "dict"):
            keys = list(val.keywords)
        else:
            continue
        out = set()
        for k in keys:
            if isinstance(k, ast.keyword):
                # dict(a=1): the key is the keyword's `arg`, not its `value`.
                if isinstance(k.arg, str):
                    out.add(k.arg)
            elif isinstance(k, ast.Constant) and isinstance(k.value, str):
                out.add(k.value)  # {"a": 1}: the key is the node itself
        return out
    return None


def attribute_problems(tree, path):
    """Check `ft.Colors.*` / `ft.Icons.*` / `ft.<helper>.*` attribute READS.

    Deliberately does NOT check attribute *assignment* (`c.bgcolor = ...`):
    flet controls accept arbitrary attribute writes through a dynamic
    __setattr__ (`ft.Container().text = "x"` succeeds and is honoured), and
    neither `hasattr`, `dir()` nor the dataclass field set reflects what the
    client renders -- an assignment check here produced 6 false positives on
    valid code, so it would have been noise.
    """
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Attribute):
            base = n.value
            if (isinstance(base.value, ast.Name) and base.value.id == "ft"
                    and base.attr in ("Colors", "Icons", "CupertinoColors",
                                      "TextStyle", "Padding", "Margin",
                                      "Alignment", "BorderSide", "Border",
                                      "MainAxisAlignment", "CrossAxisAlignment",
                                      "TextOverflow", "FontWeight", "MainAxisSize",
                                      "TextAlign", "ClipBehavior", "RoundedRectangleBorder")):
                owner = getattr(ft, base.attr, None)
                if owner is not None and not hasattr(owner, n.attr):
                    out.append((path.name, n.lineno, f"ft.{base.attr}.{n.attr}", "attribute read"))
    return out


def main():
    problems, unresolved = [], []
    for path in sorted(SRC.glob("*.py")):
        tree = ast.parse(path.read_text())
        problems.extend(attribute_problems(tree, path))
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                v = node.func.value
                if not (isinstance(v, ast.Name) and v.id == "ft"):
                    continue
                cls = getattr(ft, node.func.attr, None)
                if cls is None:
                    problems.append((path.name, node.lineno, node.func.attr, "<class missing>"))
                    continue
                if not dataclasses.is_dataclass(cls):
                    continue
                allowed = {f.name for f in dataclasses.fields(cls)}
                for kw in node.keywords:
                    if kw.arg is None:  # **splat
                        name = kw.value.id if isinstance(kw.value, ast.Name) else None
                        keys = literal_keys(fn, name) if name else None
                        if keys is None:
                            unresolved.append((path.name, node.lineno, node.func.attr))
                            continue
                        for k in sorted(keys):
                            if k not in allowed:
                                problems.append((path.name, node.lineno, node.func.attr, k))
                    elif kw.arg not in allowed:
                        problems.append((path.name, node.lineno, node.func.attr, kw.arg))
    for p in sorted(set(problems)):
        kind = p[3] if len(p) > 3 else "kwarg"
        print(f"  {p[0]}:{p[1]}  {p[2]}  not valid ({kind})")
    for u in sorted(set(unresolved)):
        print(f"  note: **splat unresolved at {u[0]}:{u[1]} ft.{u[2]}")
    n = len(set(problems))
    print(f"{n} invalid kwarg(s)/attribute(s)")
    return 1 if (problems or unresolved) else 0


if __name__ == "__main__":
    sys.exit(main())

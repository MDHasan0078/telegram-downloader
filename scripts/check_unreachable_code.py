"""Prove no code follows a terminal (raise/return) statement in any block.

This is the bug class that shipped in v1.3.0: gui.py inserted a bare `raise`
into an `except` handler without moving the success path that followed it, so
the rest of the handler became unreachable. No existing test caught it because
gui.py has zero coverage.

Committed as a test so the class cannot recur silently.
"""
import ast
import pathlib
import sys

TERMINAL = (ast.Raise, ast.Return)


def dead_after_terminal(node, path, hits):
    """Record every statement that lexically follows a terminal statement."""
    for field, value in ast.iter_fields(node):
        if isinstance(value, list) and any(isinstance(v, ast.stmt) for v in value):
            stmts = [v for v in value if isinstance(v, ast.stmt)]
            for prev, cur in zip(stmts, stmts[1:]):
                if isinstance(prev, TERMINAL):
                    hits.append((path, prev.lineno, cur.lineno))
        if isinstance(value, list):
            for item in value:
                if isinstance(item, ast.AST):
                    dead_after_terminal(item, path, hits)
        elif isinstance(value, ast.AST):
            dead_after_terminal(value, path, hits)


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent.parent
    hits = []
    for path in sorted((root / "src" / "telegram_downloader").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        dead_after_terminal(tree, path.name, hits)

    if hits:
        print("FAIL: statements following a raise/return are UNREACHABLE:")
        for path, term_line, dead_line in hits:
            print(f"  {path}:{dead_line} follows terminal statement at line {term_line}")
        print("\nEvery one of these is silently dead code. gui.py shipped a")
        print("release whose entire Add flow was unreachable this way.")
        return 1

    # Also assert the specific call sites the updater guards need, so a
    # half-restored guard cannot pass silently. See tests/test_updater_security.py.
    print("PASS: no statements follow a raise/return in any block.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
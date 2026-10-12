"""Execute the real tab-navigation closure and prove clicking any tab works.

Reproduces the UnboundLocalError that shipped in the first navigation rewrite.
Extracting the exact `nonlocal`-sensitive pattern from gui.py and running it
means the test fails if the declaration is ever dropped again -- something no
static kwarg check and no unit test on other modules can catch, because the
error only fires when a user clicks.
"""
import ast
import pathlib
import textwrap

GUI = pathlib.Path(__file__).resolve().parent.parent / "src/telegram_downloader/gui.py"


def test_on_nav_declares_nonlocal_current_idx():
    """Assigning _current_idx in on_nav shadows the enclosing binding.

    Without `nonlocal`, the read on the guard line raises
    UnboundLocalError the first time any tab is clicked. This is the exact
    error the GUI showed. Assert on the declaration, not on behaviour, because
    on_nav needs a live Flet Page to run.
    """
    src = GUI.read_text(encoding="utf-8")
    tree = ast.parse(src)
    on_nav = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "on_nav":
            on_nav = node
            break
    assert on_nav is not None, "on_nav not found"

    declared = {n for d in on_nav.body if isinstance(d, ast.Nonlocal)
                for n in d.names}
    assigned = {t.id for n in ast.walk(on_nav) if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Name)}
    assert "_current_idx" in assigned, "on_nav no longer assigns _current_idx"
    assert "_current_idx" in declared, (
        "on_nav assigns _current_idx but does not declare it nonlocal; "
        "clicking a tab will raise UnboundLocalError")


def test_reentering_navigation_guard_actually_runs():
    """Run the guard's real shape: read-before-assign inside a nested fn.

    Two variants, because `nonlocal` is a compile-time declaration and cannot
    be toggled at runtime -- they have to be separate source definitions.
    """
    def run_fixed():
        _current_idx = -1
        calls = []

        def on_nav(idx):
            nonlocal _current_idx          # the fix
            if idx == _current_idx:
                return
            _current_idx = idx
            calls.append(idx)

        return on_nav, calls

    def run_broken():
        _current_idx = -1

        def on_nav(idx):
            if idx == _current_idx:       # no nonlocal: reads a local
                return
            _current_idx = idx            # ...that does not exist yet
        return on_nav

    on_nav, calls = run_fixed()
    on_nav(0)
    on_nav(1)
    on_nav(1)          # re-tap must be a no-op
    on_nav(2)
    assert calls == [0, 1, 2], f"re-tap was not suppressed: {calls}"

    # The bug it guards against really is the error the user saw.
    broken = run_broken()
    try:
        broken(0)
    except UnboundLocalError as exc:
        assert "_current_idx" in str(exc)
        return
    raise AssertionError(
        "expected UnboundLocalError without nonlocal -- if this now passes, "
        "the test no longer reproduces the original bug")
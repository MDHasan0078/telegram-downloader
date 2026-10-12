"""Structural regressions that no functional test can catch.

gui.py shipped v1.3.0 with its entire Add flow unreachable: commit 8f91996
inserted a bare `raise` into an `except RuntimeError` handler and left the
success path after it. Nothing in the suite imports gui.py, so 137 tests passed
against an app that could not download anything.

These assertions are structural and cheap. They exist because the failure mode
is invisible: the code parses, the module imports, and the tests are green.
"""
import ast
import pathlib

import telegram_downloader.updates as upd

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "telegram_downloader"
TERMINAL = (ast.Raise, ast.Return)


def _dead_after_terminal(path):
    hits = []

    def walk(node):
        for _, value in ast.iter_fields(node):
            if isinstance(value, list):
                stmts = [v for v in value if isinstance(v, ast.stmt)]
                for prev, cur in zip(stmts, stmts[1:]):
                    if isinstance(prev, TERMINAL):
                        hits.append((prev.lineno, cur.lineno))
                for item in value:
                    if isinstance(item, ast.AST):
                        walk(item)
            elif isinstance(value, ast.AST):
                walk(value)

    walk(ast.parse(path.read_text(encoding="utf-8")))
    return hits


def test_no_unreachable_code_after_raise_or_return():
    """No statement may follow raise/return in the same block.

    Regression test for the v1.3.0 Add-flow outage. Every hit here is code
    that can never execute, usually from an edit that inserted a terminal
    statement without moving the code after it.
    """
    offenders = []
    for path in sorted(SRC.glob("*.py")):
        for term_line, dead_line in _dead_after_terminal(path):
            offenders.append(f"{path.name}:{dead_line} follows terminal at :{term_line}")
    assert not offenders, "unreachable code:\n  " + "\n  ".join(offenders)


def test_gui_add_flow_is_reachable():
    """on_fetch must still fetch previews and render them.

    Named specifically so that if this ever regresses again, the failure
    points at the user-facing flow rather than at a generic structural check.
    """
    tree = ast.parse((SRC / "gui.py").read_text(encoding="utf-8"))
    on_fetch = [n for n in ast.walk(tree)
                if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
                and n.name == "on_fetch"]
    assert len(on_fetch) == 1, "expected exactly one on_fetch"
    used = {n.id for n in ast.walk(on_fetch[0]) if isinstance(n, ast.Name)}
    used |= {n.attr for n in ast.walk(on_fetch[0]) if isinstance(n, ast.Attribute)}
    assert "fetch_preview" in used, "on_fetch no longer fetches previews"
    assert "render_previews" in used, "on_fetch no longer renders previews"


def test_u4_guards_are_absent_not_merely_uncalled():
    """The disabled anti-downgrade layer must be genuinely dormant.

    If someone reintroduces these as defined-but-uncalled, or worse re-wires
    one by accident, the test suite would still be green while the updater's
    documented behaviour changed. This pins the disabled state explicitly.
    """
    for name in ("_implausible_jump", "_installed_version_parts",
                 "_update_state_path", "_last_seen_version",
                 "_record_last_seen_version"):
        assert not hasattr(upd, name), (
            f"{name} exists; the U4 layer was disabled in ce7e0fd. If you are "
            "re-enabling it, also restore the guard block in check_for_update "
            "and the tests in test_updater_security.py.")


def test_signature_verification_still_precedes_download():
    """download_asset must verify the manifest before fetching any payload.

    This is the property that justifies disabling U4: the disabled guard was
    never on the install-safety path. If this ever inverts, that justification
    is void and the guard should come back.
    """
    import inspect
    src = inspect.getsource(upd.download_asset)
    verified = src.find("_fetch_verified_sums")
    payload = src.find("_urlopen_no_redirect")
    assert verified != -1, "download_asset no longer verifies the manifest"
    assert payload != -1, "download_asset no longer downloads anything"
    assert verified < payload, (
        "download_asset fetches the payload before verifying the manifest")
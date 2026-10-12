"""Structural regressions that no functional test can catch.

gui.py shipped v1.3.0 with its entire Add flow unreachable: commit 8f91996
inserted a bare `raise` into an `except RuntimeError` handler and left the
success path after it. Nothing in the suite imports gui.py, so 137 tests passed
against an app that could not download anything.

These assertions are structural and cheap. They exist because the failure mode
is invisible: the code parses, the module imports, and the tests are green.
"""
import ast
import base64
import pathlib
import re

import telegram_downloader.updates as upd
from telegram_downloader.gui import app_logo_src

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


def test_gui_nav_destinations_match_the_views_list():
    """Navigation indexes into `views`, so the two lists must stay aligned.

    deps_view was implemented and documented but never added to the nav, so
    the page was unreachable while the README told users to install ffmpeg
    through it. Both lists are positional: adding a view without a matching
    destination shifts every later tab, and adding a destination without a
    view makes navigation raise IndexError at runtime.
    """
    src = (SRC / "gui.py").read_text(encoding="utf-8")
    views_block = re.search(r"views = \[(.*?)\]\n", src, re.S)
    assert views_block, "could not find the views list in gui.py"
    entries = re.findall(r"(\w+)\(page\)", views_block.group(1))
    assert entries == ["downloads_view", "queue_view", "settings_view"], (
        f"unexpected views list: {entries}")

    # Navigation is a top row of chips built from primitives. flet 0.86.5's
    # own tab controls cannot be driven the way _goto() needs, so the chip
    # count is what keeps navigation aligned with `views`.
    chips = re.findall(r"^\s+(?:queue_chip = )?_make_chip\((\d+), \"(\w+)\"",
                       src, re.M)
    # Compare by index, not source order: `queue_chip` is hoisted out of the Row
    # so refresh_queue() can reach it, so it appears first in the source.
    by_index = {int(i): label for i, label in chips}
    assert sorted(by_index) == list(range(len(entries))), (
        f"tab chips are at indices {sorted(by_index)} but there are "
        f"{len(entries)} views; _make_chip indexes into `views`, so a gap or "
        "a stale index shows the wrong page or raises IndexError")
    assert [by_index[i] for i in range(len(entries))] == \
        ["Add", "Queue", "Settings"], f"unexpected tab labels: {by_index}"

    # The side rail and the bottom nav bar are gone; if either comes back the
    # layout will double up with the top chips.
    assert "NavigationRail(" not in src, "NavigationRail should be removed"
    assert "NavigationBar(" not in src, "NavigationBar should be removed"
    assert "page.navigation_bar" not in src, "page.navigation_bar should be unset"

    # Dependencies is a Settings section now, not a top-level tab.
    assert "deps_view(page)]" not in src, \
        "Dependencies should not be a top-level tab"


def test_tab_chips_are_keyed_by_index_not_construction_order():
    """A click must highlight the chip that was clicked.

    `queue_chip` is constructed BEFORE the Row, because refresh_queue() needs
    a handle on it. So _tab_chips is [Queue, Add, Settings] while the screen
    shows [Add, Queue, Settings]. Styling keyed on enumerate() order therefore
    highlighted the wrong chip -- clicking "Add" lit up "Queue", which reads as
    the navigation being broken.

    The fix tags each chip with data=idx and matches on that. This test exists
    so the ordering assumption can never silently come back, because a
    position-based comparison looks correct right up until one chip is hoisted.
    """
    src = (SRC / "gui.py").read_text(encoding="utf-8")
    assert "data=idx," in src, "chips must carry their own index (data=idx)"

    selection = src[src.index("def _set_selected_index"):
                    src.index("def on_nav")]
    assert "chip.data == idx" in selection, \
        "selection must match on chip.data, not list position"
    assert "enumerate(_tab_chips)" not in selection, \
        "selection must not depend on construction order"

    # Construction order really does differ from visual order -- that is the
    # whole reason position-based matching was wrong.
    builds = re.findall(r'^\s+(?:queue_chip = )?_make_chip\((\d+), "(\w+)"',
                        src, re.M)
    order = [int(i) for i, _ in builds]
    assert order != [0, 1, 2], (
        "chips are built in visual order; this test's premise (and the "
        "data=idx fix) assumed they are not")
    by_index = {int(i): label for i, label in builds}
    assert [by_index[i] for i in range(3)] == ["Add", "Queue", "Settings"], \
        f"index->label mapping is wrong: {by_index}"


def test_readme_documents_only_real_gui_pages():
    """The README must not advertise a page the user cannot reach."""
    readme = (SRC.parent.parent / "README.md").read_text(encoding="utf-8")
    src = (SRC / "gui.py").read_text(encoding="utf-8")
    views_block = re.search(r"views = \[(.*?)\]\n", src, re.S).group(1)
    for name, label in (("deps_view", "Dependencies"),
                        ("queue_view", "Queue"),
                        ("settings_view", "Settings")):
        # Dependencies is reachable as a Settings section rather than a tab.
        if name == "deps_view":
            assert "deps_section(page)" in src, \
                "Dependencies must be reachable, as a Settings section"
            continue
        assert name in views_block, f"{name} is not wired into the nav"
        if label in readme:
            assert name in views_block, (
                f"README mentions {label!r} but {name} is not reachable")


def test_download_location_is_reachable_from_the_window_chrome():
    """The download folder must be changeable without entering Settings.

    It is the field most likely to need changing, so it belongs in the always-
    visible bottom bar rather than behind a navigation step. It must also go
    through ensure_download_dir() before being stored, so the bottom bar can
    never display a folder that download time would reject.
    """
    src = (SRC / "gui.py").read_text(encoding="utf-8")
    assert "_pick_download_dir" in src, "no download-folder picker is wired up"
    assert "dl_bar" in src, "no bottom download-location bar"
    body = src[src.index("async def _pick_download_dir"):
               src.index("dl_bar = ft.Container")]
    assert "ensure_download_dir(st)" in body, (
        "the picker must validate the folder before saving it")
    assert "get_directory_path" in body, "must use a real folder picker"


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


def test_title_bar_uses_the_packaged_app_logo():
    """The title bar must show the real shipped logo, not a stand-in.

    The SVG is packaged as package-data so every install renders the
    same mark. app_logo_src() reads it back as a self-contained data
    URI; if that ever regresses to None the AppBar falls back to a
    drawn arrow, which is why this pins the real asset directly.
    """
    src = app_logo_src()
    assert src is not None, "packaged assets/icon.svg could not be read"
    assert src.startswith("data:image/svg+xml;base64,"), (
        "logo is not a self-contained SVG data URI")

    svg = base64.b64decode(src.split(",", 1)[1]).decode("utf-8")
    assert svg.lstrip().startswith("<svg"), "asset is not an SVG"
    # The brand colours must survive: Telegram-blue tile, dark glyph.
    assert "#2AABEE" in svg, "logo lost its blue tile"
    assert "#0F1117" in svg, "logo lost its dark glyph"

    # And the AppBar logo must be built from ft.Image (the real
    # asset), with the primitives only as a fallback branch.
    # app_logo is assigned in its own statement, so search the
    # whole module for "app_logo = ft.Image(...)".
    tree = ast.parse((SRC / "gui.py").read_text(encoding="utf-8"))
    logo_images = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "app_logo"
                for t in n.targets)
        and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Attribute)
        and n.value.func.attr == "Image"
    ]
    assert logo_images, "app_logo is not built from the packaged ft.Image"


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
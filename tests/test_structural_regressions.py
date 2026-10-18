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
import builtins
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
    # The chip-into-view alignment is what matters, so match ANY variable name
    # being assigned a chip. Hardcoding "queue_chip =" only made this test
    # break when a chip was renamed, which is not the invariant worth pinning.
    chips = re.findall(r"^\s+(?:\w+ = )?_make_chip\((\d+), \"(\w+)\"",
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
    builds = re.findall(r'^\s+(?:\w+ = )?_make_chip\((\d+), "(\w+)"',
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
    # Slice up to the assignment regardless of which builder constructs it
    # (it is _bar(...) now, not ft.Container(...)); the invariant is the
    # validation, not the widget class.
    body = src[src.index("async def _pick_download_dir"):
               src.index("dl_bar = ")]
    assert "ensure_download_dir(st)" in body, (
        "the picker must validate the folder before saving it")
    assert "get_directory_path" in body, "must use a real folder picker"


def test_gui_add_flow_is_reachable():
    """The Add flow must end up in the queue, and it must start there too.

    This test originally pinned the five-step preview flow: on_fetch had to
    fetch previews and render them. That flow was the bug it was watching for
    -- it shipped broken in v1.3.0 -- but the fix was to delete the step, not
    to restore it, so the assertion now pins the shape that replaced it.

    on_add must put the pasted URLs into the queue and start the worker. If
    that ever inverts again -- items built but never queued, or queued but
    never started -- the app looks fine and downloads nothing, which is exactly
    how the v1.3.0 regression presented.
    """
    tree = ast.parse((SRC / "gui.py").read_text(encoding="utf-8"))
    on_add = [n for n in ast.walk(tree)
              if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
              and n.name == "on_add"]
    assert len(on_add) == 1, "expected exactly one on_add"
    used = {n.id for n in ast.walk(on_add[0]) if isinstance(n, ast.Name)}
    used |= {n.attr for n in ast.walk(on_add[0]) if isinstance(n, ast.Attribute)}
    assert "split_urls" in used, "on_add no longer parses the pasted links"
    assert "extend" in used, "on_add no longer adds the items to the queue"
    assert "start_queue" in used, (
        "on_add queues the items but never starts the worker, so the user "
        "pastes a link, presses Add, and nothing happens")


def test_the_queue_worker_names_files_from_the_fetched_message():
    """A queued item with no title must still get a real filename.

    Adding no longer previews anything, so DownloadItem arrives with an empty
    title. The worker names the file from the message it fetches; without that
    every download lands as msg-12345.mp4.
    """
    text = GUI_PY.read_text(encoding="utf-8")
    assert "auto_title_from_message(msg, chat)" in text, (
        "the worker no longer derives the filename from the message it "
        "fetches; files will be saved as msg-<id>")


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
    # Pin the mark's PROPERTIES, not the exact artwork of the week. These
    # three are what keep it from reading as a clipboard icon:
    #   1. a rounded squircle tile, never a hard square
    #   2. a ground from the app's cyan-aqua family -- Telegram's blue, one
    #      step warmer: recognisably related, plainly not the same
    #   3. a light glyph on that ground, for contrast at 20px in the top bar
    assert 'rx="17"' in svg or "rx='17'" in svg, (
        "the logo tile is no longer the 17px-radius squircle of 'Descent'")
    assert "#2AC9D6" in svg, (
        "logo lost its teal ground; the mark must stay in the app's "
        "cyan-aqua accent family")
    assert "#FFFFFF" in svg.upper(), (
        "the logo glyph is not white; at 20px in the top bar it has to read "
        "against the teal tile")

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

# ---------------------------------------------------------------------------
# Flet layout traps that fail SILENTLY.
#
# Both of these render as "a correct-height blank band" rather than raising, so
# nothing in the normal test run and nothing in the server log reveals them.
# They were found by screenshotting the running app, which is exactly why they
# are pinned here now.
# ---------------------------------------------------------------------------

GUI_PY = SRC / "gui.py"


def _flet_call_name(node):
    """'ft.Column' / 'ft.TextField' / 'TextField' -> the trailing name."""
    if not isinstance(node, ast.Call):
        return None
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return None


def test_no_textfield_expands_under_an_unbounded_height_column():
    """A TextField with expand=True is only legal under a ListView.

    This is the precise rule behind two silent failures found by screenshotting
    the running app.

    A scrolling ft.Column hands its children UNBOUNDED height. An Expanded child
    under unbounded height cannot be laid out, so Flutter drops the subtree: the
    field, and everything sharing its Row, render nothing -- while the parent
    still reserves their correct height. The only symptom is a blank band where
    a control should be, with no exception anywhere.

      - The Add tab's URL box did this: the field and all three of its buttons
        vanished, leaving a ~157px gap under the page title.
      - The Settings download-folder field did the same further down the page.

    Under a ft.ListView the height IS bounded, so expand=True is correct there
    and is used deliberately (the preview title fields). The rule is about the
    ancestor's constraint, not about the property being wrong on its own.

    Confirmed with a minimal repro: the identical tree renders with expand
    removed and renders nothing, at the same height, with expand set.
    """
    tree = ast.parse(GUI_PY.read_text(encoding="utf-8"))
    parent = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node

    def kwarg(call, name):
        for kw in call.keywords:
            if kw.arg == name:
                return kw.value
        return None

    def scrolls_unbounded(node):
        """True if node is a Column that scrolls -> unbounded child height."""
        name = _flet_call_name(node)
        if name != "Column":
            return False
        sc = kwarg(node, "scroll")
        return sc is not None and not (isinstance(sc, ast.Constant)
                                       and sc.value is None)

    offenders = []
    for node in ast.walk(tree):
        if _flet_call_name(node) != "TextField":
            continue
        exp = kwarg(node, "expand")
        if exp is None or (isinstance(exp, ast.Constant)
                           and exp.value is False):
            continue
        cur, chain = parent.get(node), []
        while cur is not None:
            chain.append(_flet_call_name(cur))
            cur = parent.get(cur)
        if any(scrolls_unbounded(c) for c in chain
               if isinstance(c, ast.Call)):
            offenders.append((node.lineno, "under a scrolling ft.Column"))
    assert not offenders, (
        f"ft.TextField(expand=...) {offenders}. Under a scrolling Column the "
        "height is unbounded, so Flutter renders nothing at all. Use "
        "min_lines/max_lines, and let the parent stretch it instead.")


def test_grow_helper_exists_and_sets_expand():
    """_grow() must exist, and it must be what marks a Row child as expanding.

    A Row child that neither expands nor sets an explicit width is laid out at
    ZERO width, while the Row still reserves its correct height. On the Add tab
    that made the URL field and all three of its buttons disappear, leaving a
    ~157px blank band -- a silent failure, not an error.

    Asserting on the helper rather than on a variable name is deliberate: three
    tests in this file were previously pinned to `queue_chip = ` and
    `dl_bar = ft.Container` and broke on an unrelated rename.
    """
    text = GUI_PY.read_text(encoding="utf-8")
    assert "def _grow(" in text, (
        "_grow() is gone. A Row child with neither expand nor an explicit width "
        "collapses to zero width and renders nothing.")
    assert "control.expand = flex" in text, (
        "_grow() exists but no longer sets expand; it is now a no-op.")
    assert len(re.findall(r"\b_grow\(", text)) >= 2, (
        "_grow() is defined but never called; the Row-children regression is "
        "back.")


def test_theme_comes_from_the_ui_module_not_inline_colours():
    """Colour lives in ui.py; gui.py must not hand-build a theme.

    Two palettes drifted apart while both existed -- an amber scheme in a local
    PALETTES dict and a blue-slate ColorScheme beside it -- which is how the app
    ended up with an accent that matched nothing. ui.py is the single source.
    """
    text = GUI_PY.read_text(encoding="utf-8")
    assert "from . import ui" in text, "gui.py no longer imports the ui module"
    assert "ui.theme(dark=True)" in text and "ui.theme(dark=False)" in text, (
        "gui.py is not building both themes through ui.theme()")
    assert "PALETTES" not in text.replace(
        "to carry its own PALETTES dict plus a hand-built ColorScheme", ""
    ), ("a PALETTES dict is back in gui.py; colour must have one home")


def test_cards_are_only_for_list_items_and_named_cards():
    """section() must be gone and group() must be bare unless asked.

    Wrapping every Settings group in its own bordered card was the reason the
    GUI read as broken: three or four full-width boxes of equal weight, each
    with its own title and hairline, so nothing had a clear primary area. The
    reference allows cards only for list rows and the path bar.
    """
    text = GUI_PY.read_text(encoding="utf-8")
    assert "def section(" not in text, (
        "section() is back; it is the card-soup helper that was replaced by "
        "group().")
    assert "def group(" in text
    # group() must return a bare Column unless card=True
    m = re.search(r"def group\(.*?\n    def ", text, re.S)
    assert m, "could not locate group()"
    body = m.group(0)
    assert "if not card:" in body and "return col" in body, (
        "group() is not bare by default")


def _own_scope_bindings(fn):
    """Names bound in fn's own scope, NOT descending into nested scopes."""
    out = set()

    def collect(node):
        stack = [node]
        while stack:
            n = stack.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                for a in n.names:
                    out.add((a.asname or a.name).split(".")[0])
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                out.add(n.id)
            elif isinstance(n, ast.arg):
                out.add(n.arg)
            elif isinstance(n, ast.ExceptHandler) and n.name:
                out.add(n.name)
            stack.extend(ast.iter_child_nodes(n))

    for stmt in fn.body:
        collect(stmt)
    return out


def _names_loaded_in(fn):
    loaded = set()
    stack = [fn]
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            loaded.add(n.id)
        stack.extend(ast.iter_child_nodes(n))
    return loaded


def test_refresh_queue_does_not_touch_queue_view_locals():
    """refresh_queue() must not reference a name bound only in queue_view().

    The two are siblings inside run_gui, so Python resolves refresh_queue's
    free names against run_gui's scope -- anything defined inside queue_view is
    invisible to it and raises NameError at runtime.

    That is not hypothetical. refresh_queue() contained `header.value = ...`
    for an empty queue; `header` is a queue_view local. It never fired only
    because the function used to bail out at the top on every call. Fixing that
    (the queue genuinely never repopulated, see the _main_page change) turned
    dead code into a crash on every empty-queue refresh:

        NameError: name 'header' is not defined

    The count does not need a queue_view local: it is set above, from the
    shared queue_header_txt["ctrl"] handle.
    """
    tree = ast.parse(GUI_PY.read_text(encoding="utf-8"))
    fns = {n.name: n for n in ast.walk(tree)
           if isinstance(n, ast.FunctionDef)
           and n.name in ("refresh_queue", "queue_view", "run_gui")}
    assert set(fns) == {"refresh_queue", "queue_view", "run_gui"}, sorted(fns)

    run_own = _own_scope_bindings(fns["run_gui"])
    leaked = sorted((_own_scope_bindings(fns["queue_view"])
                     & _names_loaded_in(fns["refresh_queue"]))
                    - run_own - set(dir(builtins)))
    assert not leaked, (
        f"refresh_queue() references {leaked}, which is bound only inside "
        "queue_view(). The two are siblings under run_gui, so this is a "
        "NameError at runtime -- it just stays hidden while the line is on a "
        "path nothing takes.")


def test_the_shared_header_handle_is_what_updates_the_count():
    """The Queue count must go through queue_header_txt, not a local Text.

    Checked on the AST rather than as a substring: a prose comment that *names*
    the bug is worth keeping, and a text search cannot tell an assignment from
    a sentence about one.
    """
    text = GUI_PY.read_text(encoding="utf-8")
    assert 'queue_header_txt["ctrl"]' in text, (
        "refresh_queue() no longer updates the header through the shared "
        "queue_header_txt handle")

    tree = ast.parse(text)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "refresh_queue")
    targets = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if (isinstance(t, ast.Attribute)
                        and isinstance(t.value, ast.Name)):
                    targets.add(t.value.id)
        elif isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Attribute):
            targets.add(n.target.value.id)
    assert "header" not in targets, (
        "refresh_queue() assigns to `header`, which is a queue_view local and "
        "is not visible from here -> NameError on every empty-queue refresh.")


def _hex_rgb(value: str):
    v = value.lstrip("#")
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))


def _luma(rgb):
    """Relative luminance, sRGB coefficients."""
    r, g, b = (c / 255 for c in rgb)
    f = lambda c: c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def test_surface_ramp_is_ordered_in_both_modes():
    """The five surface tones must form the ramp the design language names.

    The vocabulary is positional, not decorative: wells are *below* the page
    (the link field, a progress trough, the nav strip) and raised / floating
    are *above* it (the selected nav pill, the bottom bar, dialogs). So
    luminance must rise in one direction -- the same direction -- in both
    modes, and "below the page" / "above the page" must be strict, not
    approximate. Scramble this and the tones stop meaning anything: that is
    exactly when the app starts reading as a stack of boxes.
    """
    from telegram_downloader import ui

    order = ("well_deep", "well", "page", "raised", "floating")
    for name, tokens in (("dark", ui.DARK), ("light", ui.LIGHT)):
        lum = {k: _luma(_hex_rgb(tokens[k])) for k in order}
        # Monotonic overall. A tie is only tolerable where a mode has nothing
        # higher to reach: light's floating and raised are both #FFFFFF.
        for lo, hi in zip(order, order[1:]):
            assert lum[lo] <= lum[hi], (
                f"{name}: {lo} {tokens[lo]} is lighter than {hi} "
                f"{tokens[hi]} -- the surface ramp is out of order")
        # Positional and strict: a well is below the page, raised is above it.
        assert lum["well_deep"] < lum["page"] and lum["well"] < lum["page"], (
            f"{name}: a well is not below the page; input troughs would "
            "read as raised panels")
        assert lum["page"] < lum["raised"], (
            f"{name}: raised is not above the page; bars and pills would "
            "sink instead of sitting on top")


def test_every_surface_role_the_gui_uses_is_actually_set():
    """No ft.Colors.SURFACE_* role may fall back to a generated default.

    The shell Container that fills the page body paints with
    SURFACE_CONTAINER_LOWEST, and every card border uses OUTLINE_VARIANT.
    Neither was in the ColorScheme, so Flutter derived both from the seed:
    the body came out near-black and the borders came from the accent seed.
    That silently overrode scaffold_bgcolor -- the token that names the page
    colour was never reaching the screen, which is why changing it appeared to
    do nothing.

    A generated default here is always wrong, whatever the seed.
    """
    from telegram_downloader import ui
    import flet as ft

    scheme = ui.theme(dark=True).color_scheme
    for role in ("surface", "surface_container_lowest", "surface_container_low",
                 "surface_container", "surface_container_high",
                 "surface_container_highest", "surface_dim", "surface_bright",
                 "outline", "outline_variant", "on_surface",
                 "on_surface_variant"):
        value = getattr(scheme, role, None)
        assert value, f"ColorScheme.{role} is unset; it will be generated"


# ---------------------------------------------------------------------------
# Minimalism. These are the things that made the app look busy while nothing
# was actually wrong: a page title duplicating the tab label, the same empty
# message printed twice, controls rendered over an empty list, and per-row
# decoration restating what the row already showed.
# ---------------------------------------------------------------------------

def test_no_page_header_helper():
    """Each tab used to open with an icon tile + 16px title + subtitle.

    The tab strip directly above already says which tab you are on, so this
    printed "Queue" / "Queue" and "Settings" / "Settings" -- a full line of
    duplicate chrome on every screen, on top of a one-line explanation of
    something self-evident. The reference has no page title at all: the tab
    underline is the page title.
    """
    text = GUI_PY.read_text(encoding="utf-8")
    assert "def h1(" not in text, (
        "the page-header helper is back; the tab label already names the page")


def test_empty_state_text_is_not_duplicated():
    """No string may say "No previews yet" anywhere.

    The Preview header rendered unconditionally and hardcoded
    count_txt.value = "No previews yet", while the empty state below it said
    the same thing -- the sentence appeared twice on an empty Add tab. The
    preview step is gone entirely now, so the phrase has no reason to exist.
    """
    text = GUI_PY.read_text(encoding="utf-8")
    tree = ast.parse(text)
    offenders = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
                and "No previews yet" in node.value.value):
            offenders.append(node.lineno)
    assert not offenders, (
        f"'No previews yet' assigned at line(s) {offenders}; the empty state "
        "already prints it. The header row is hidden while the list is empty, "
        "so there is nothing to keep in sync.")


def test_queue_summary_counters_carry_no_background_fill():
    """The "2 active / 1 queued / 1 done" line must be plain text.

    These were four bgcolor TextSpans -- four small coloured boxes in a
    toolbar, the loudest thing on the page before you reached the rows. Only
    "need attention" keeps its colour, because that one is asking for
    something.
    """
    text = GUI_PY.read_text(encoding="utf-8")
    tree = ast.parse(text)
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name != "TextStyle":
                continue
            for kw in node.keywords:
                if kw.arg == "bgcolor":
                    offenders.append(kw.value.lineno)
    assert not offenders, (
        f"TextStyle(bgcolor=...) at line(s) {offenders}; queue summary counters "
        "are text, not chips.")


def test_queue_rows_do_not_restate_the_progress_bar():
    """No percentage readout and no per-row subtitle under the title.

    Both duplicated what the row already showed: the bar is directly under the
    title and carries the same number to better precision, and the format is
    visible in the filename while the status line carries the size for anything
    in flight.
    """
    text = GUI_PY.read_text(encoding="utf-8")
    assert "def pct_pill(" not in text, (
        "pct_pill is back; the progress bar already shows the percentage")
    assert "subtitle_bits" not in text, (
        "the per-row '.mp4 - 1.3 GB' subtitle is back")

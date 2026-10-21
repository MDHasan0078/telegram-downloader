"""The Kotlin's own API, cross-checked without an Android SDK.

The native app is 2,900 lines of Kotlin that no compiler in this
container has ever seen, and the CI round trip is twelve minutes. These
checks answer the questions that do NOT need a compiler -- the ones
where the code refers to something the project itself declares:

  - `vm.someMethod` must be a member of TgViewModel;
  - `c.someColour` must be a member of the theme's colour class;
  - `Space.something` and `Type.something` must be declared;
  - a function the Kotlin calls on the Python bridge must exist in
    bridge.py, with the same name.

The last one is the most valuable: a name that does not exist is not a
compile error in Kotlin (the call is dynamic, through PyObject), it is a
runtime crash on the first tap. The bridge API is therefore checked
against BOTH sides.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
KOTLIN = REPO / "android/app/src/main/kotlin/com/tgdownloader/telegram_downloader"
BRIDGE = REPO / "src/telegram_downloader/bridge.py"


def _kotlin_sources():
    return sorted(KOTLIN.rglob("*.kt"))


def _declarations():
    """Every name the project declares, in any of its Kotlin files."""
    names = set()
    pattern = re.compile(
        r"^\s*(?:@Composable\s+)?(?:private |internal |public |open |override )*"
        r"(?:fun|val|var|class|object|enum class|data class|interface|sealed class)"
        r"\s+([A-Za-z0-9_]+)", re.M)
    for path in _kotlin_sources():
        names.update(pattern.findall(path.read_text(encoding="utf-8")))
    return names


def test_there_is_kotlin_to_check():
    assert _kotlin_sources(), "no Kotlin found under android/"


def test_every_vm_call_exists():
    """vm.<name> must be a member of TgViewModel.

    A typo here is a crash when the user taps the thing, not a build
    failure, so nothing else would catch it.
    """
    vm = (KOTLIN / "TgViewModel.kt").read_text(encoding="utf-8")
    members = set(re.findall(
        r"^\s*(?:private |internal |public |open |override )*"
        r"(?:fun|val|var)\s+([A-Za-z0-9_]+)", vm, re.M))
    assert members, "TgViewModel declares no members at all?"
    used = set()
    for path in _kotlin_sources():
        used.update(re.findall(r"\bvm\.([a-zA-Z_][A-Za-z0-9_]*)",
                               path.read_text(encoding="utf-8")))
    missing = sorted(used - members)
    assert not missing, (
        f"the UI calls vm.{missing}, which TgViewModel does not declare")


def test_every_theme_colour_exists():
    """c.<name> comes from tgColors(); the colours are declared in Theme.kt."""
    theme = (KOTLIN / "Theme.kt").read_text(encoding="utf-8")
    declared = set(re.findall(
        r"^\s*(?:private |internal |public |open |override )*val\s+([A-Za-z0-9_]+)",
        theme, re.M))
    used = set()
    for path in _kotlin_sources():
        used.update(re.findall(r"\bc\.([a-zA-Z_][A-Za-z0-9_]*)",
                               path.read_text(encoding="utf-8")))
    missing = sorted(used - declared)
    assert not missing, (
        f"the UI reads c.{missing}, which Theme.kt does not declare")


def test_every_space_and_type_token_exists():
    """Space.s1 and Type.body are scale tokens from Theme.kt."""
    theme = (KOTLIN / "Theme.kt").read_text(encoding="utf-8")
    declared = set(re.findall(
        r"^\s*(?:private |internal |public |open |override )*val\s+([A-Za-z0-9_]+)",
        theme, re.M))
    used = set()
    for path in _kotlin_sources():
        text = path.read_text(encoding="utf-8")
        used.update(re.findall(r"\bSpace\.([a-zA-Z_][A-Za-z0-9_]*)", text))
        used.update(re.findall(r"\bType\.([a-zA-Z_][A-Za-z0-9_]*)", text))
    missing = sorted(used - declared)
    assert not missing, (
        f"the UI uses Space/Type.{missing}, which Theme.kt does not declare")


def test_every_bridge_call_has_a_python_function():
    """The Kotlin calls the Python bridge by name, dynamically.

    A name that does not exist in bridge.py is NOT a compile error --
    the call goes through PyObject -- it is a crash on the first tap.
    """
    python_names = set(re.findall(r"^def\s+([a-z_][a-z0-9_]*)",
                                  BRIDGE.read_text(encoding="utf-8"), re.M))
    assert python_names, "bridge.py declares no functions"
    used = set()
    for path in _kotlin_sources():
        used.update(re.findall(r'call\("([a-z_][a-z0-9_]*)"',
                               path.read_text(encoding="utf-8")))
        used.update(re.findall(r'callVoid\("([a-z_][a-z0-9_]*)"',
                               path.read_text(encoding="utf-8")))
    missing = sorted(used - python_names)
    assert not missing, (
        f"the Kotlin calls bridge.{missing}, which bridge.py does not define")


def test_the_bridge_init_signature_matches_the_call():
    """init(files_dir, download_dir, cache_dir) -- all three, in that order."""
    signature = re.search(r"^def init\(([^)]*)\)",
                          BRIDGE.read_text(encoding="utf-8"), re.M)
    assert signature, "bridge.py has no init()"
    params = [p.split(":")[0].split("=")[0].strip()
              for p in signature.group(1).split(",")]
    assert params[:3] == ["files_dir", "download_dir", "cache_dir"], (
        f"init() takes {params}, but the Kotlin passes the app's files dir, "
        f"the download dir and the cache dir in that order")

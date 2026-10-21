"""The Android app's resources, checked without an Android SDK.

Every one of these was a build failure that only a CI runner could find,
because this container has no Gradle, no Android SDK and no Kotlin
compiler. Each one is a mistake that a plain XML parse or a name lookup
catches in a second:

  - the manifest used a double hyphen inside an XML comment, which is
    illegal and made the manifest merger fail with "Error parsing
    ...AndroidManifest.xml";
  - the manifest named an activity and an application class that did
    not exist;
  - the manifest pointed at a drawable, a style and a string that were
    not in res/.

The last two are resource-linker errors at build time, and they are
exactly as invisible from a laptop as the first one was.
"""
import pathlib
import re
import xml.etree.ElementTree as ET

import pytest

ANDROID = pathlib.Path(__file__).resolve().parent.parent / "android"
MANIFEST = ANDROID / "app/src/main/AndroidManifest.xml"
RES = ANDROID / "app/src/main/res"
KOTLIN = ANDROID / "app/src/main/kotlin/com/tgdownloader/telegram_downloader"
ANDROID_NS = "{http://schemas.android.com/apk/res/android}"


def _xml_files():
    return sorted(ANDROID.rglob("*.xml"))


def test_there_are_xml_files_to_check():
    """A check that silently finds nothing is worse than no check."""
    assert _xml_files(), "no XML files found under android/"


@pytest.mark.parametrize("path", _xml_files(), ids=lambda p: p.name)
def test_every_xml_file_parses(path):
    """XML comments may not contain a double hyphen. Ever.

    That one character broke the manifest merger, and the error it
    produced ("Error parsing ...AndroidManifest.xml") says nothing about
    what is actually wrong.
    """
    try:
        ET.parse(path)
    except ET.ParseError as exc:
        pytest.fail(f"{path.name} is not well-formed XML: {exc}")


def test_the_manifest_names_classes_that_exist():
    """android:name=".Foo" is a class reference, and it must resolve."""
    root = ET.parse(MANIFEST).getroot()
    names = set()
    for element in root.iter():
        value = element.get(f"{ANDROID_NS}name")
        if value and value.startswith("."):
            names.add(value[1:])
    assert names, "the manifest declares no classes"
    for name in names:
        assert (KOTLIN / f"{name}.kt").is_file(), (
            f"the manifest names .{name} but there is no {name}.kt")


def test_the_manifest_only_references_resources_that_exist():
    """@drawable/x, @style/y and @string/z must all be in res/."""
    root = ET.parse(MANIFEST).getroot()
    referenced = set()
    for element in root.iter():
        for value in element.attrib.values():
            # A resource name may contain dots: @style/Theme.TelegramDownloader
            referenced.update(re.findall(r"@(drawable|style|string|mipmap|color)/"
                                        r"([A-Za-z0-9_.]+)", value))

    defined = {
        "drawable": {p.stem for p in (RES / "drawable").glob("*.xml")},
        "style": set(re.findall(r'<style name="([^"]+)"',
                                (RES / "values" / "themes.xml").read_text())),
        "string": set(re.findall(r'<string name="([^"]+)"',
                                 (RES / "values" / "strings.xml").read_text())),
        "mipmap": set(),
        "color": set(),
    }
    for kind, name in sorted(referenced):
        assert name in defined.get(kind, set()), (
            f"the manifest references @{kind}/{name}, which res/ does not "
            f"define")


# The flags android:configChanges accepts. Taken from AAPT's own list, which
# it prints in the error when a flag is not one of these -- and when one is
# not, AAPT reports the WHOLE list as incompatible, so the message names
# the symptom rather than the typo.
CONFIG_CHANGES_FLAGS = {
    "assetsPaths", "colorMode", "density", "fontScale",
    "fontWeightAdjustment", "grammaticalGender", "keyboard", "keyboardHidden",
    "layoutDirection", "locale", "mcc", "mnc", "navigation", "orientation",
    "resourcesUnused", "screenLayout", "screenSize", "smallestScreenSize",
    "touchscreen", "uiMode",
}


def test_every_config_changes_flag_is_real():
    """`screenSizeSmallest` is not a flag. `smallestScreenSize` is.

    AAPT's error for it is unhelpful: it lists every flag it knows and
    says the whole set is "incompatible with attribute configChanges",
    which reads as if the whole list were wrong.
    """
    root = ET.parse(MANIFEST).getroot()
    for element in root.iter():
        value = element.get(f"{ANDROID_NS}configChanges")
        if not value:
            continue
        for flag in value.split("|"):
            assert flag in CONFIG_CHANGES_FLAGS, (
                f"android:configChanges names {flag!r}, which is not a flag. "
                f"The real one is 'smallestScreenSize'.")

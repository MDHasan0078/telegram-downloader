"""The Android config-dir fix, and the trap it walks into.

The published APK was a red error screen on launch because the app tried
to create /data/.config/telegram-downloader -- /data is the app's
passwd home and the app cannot write there -- and config.py refuses to
run with an unwritable config dir.

These tests pin the fix and, more importantly, pin WHY it has to run
before config.py is imported.
"""
import os
import pathlib
import stat
import subprocess
import sys
import textwrap

import pytest

from telegram_downloader import _android_paths

REPO = pathlib.Path(__file__).resolve().parent.parent


def test_importing_the_helper_does_not_import_config():
    """The fix has to be usable BEFORE config.py is imported.

    config.py computes CONFIG_DIR at import time, so a helper that
    dragged config.py in could not be used to change where the settings
    live. The package __init__ is only a version string, which is what
    makes this possible.
    """
    code = textwrap.dedent("""
        import sys
        from telegram_downloader import _android_paths
        assert "telegram_downloader.config" not in sys.modules, \\
            "importing the helper pulled config.py in, and CONFIG_DIR " \\
            "would already have been computed"
        print("ok")
    """)
    out = subprocess.run([sys.executable, "-c", code],
                         capture_output=True, text=True,
                         cwd=str(REPO),
                         env={**os.environ,
                              "PYTHONPATH": str(REPO / "src")})
    assert out.returncode == 0, out.stdout + out.stderr
    assert "ok" in out.stdout


def test_it_points_xdg_config_home_at_a_writable_directory(tmp_path, monkeypatch):
    """The directory it chooses must be one this process can write to."""
    good = tmp_path / "app-files"
    good.mkdir()
    monkeypatch.setattr(_android_paths, "writable_dirs", lambda: [str(good)])

    chosen = _android_paths.point_config_at_writable_storage()

    assert chosen is not None
    assert os.environ["XDG_CONFIG_HOME"] == chosen
    assert pathlib.Path(chosen).is_dir()
    # config.py refuses a world- or group-writable config dir, so this has
    # to be 0700 -- not merely writable.
    mode = stat.S_IMODE(os.stat(chosen).st_mode)
    assert mode == 0o700, f"the config dir is {oct(mode)}, config.py wants 0700"


def test_it_skips_a_directory_it_cannot_write_to(tmp_path, monkeypatch):
    """A directory that cannot hold a file is not a candidate.

    This is the /data case: the app's passwd home is a real directory
    and it is not writable by the app's uid, so the write probe is what
    rejects it. The probe is mocked rather than chmod'ed because the
    suite may run as root, and root CAN write to a 0o500 directory --
    which would make a permission-based test pass for the wrong reason.
    """
    blocked = tmp_path / "data"
    blocked.mkdir()
    good = tmp_path / "good"
    good.mkdir()

    real_write_text = pathlib.Path.write_text

    def fake_write_text(self, *a, **kw):
        if self.parent == blocked:
            raise PermissionError(13, "Permission denied", str(self))
        return real_write_text(self, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "write_text", fake_write_text)
    monkeypatch.setattr(_android_paths, "writable_dirs",
                        lambda: [str(blocked), str(good)])

    chosen = _android_paths.point_config_at_writable_storage()

    assert chosen is not None
    assert str(good) in chosen
    assert str(blocked) not in chosen


def test_it_skips_something_that_is_not_a_directory(tmp_path, monkeypatch):
    """A file is not a config dir, and a path that does not exist is not
    one either."""
    a_file = tmp_path / "not-a-dir"
    a_file.write_text("x")
    missing = tmp_path / "missing"
    good = tmp_path / "good"
    good.mkdir()

    monkeypatch.setattr(_android_paths, "writable_dirs",
                        lambda: [str(a_file), str(missing), str(good)])

    chosen = _android_paths.point_config_at_writable_storage()
    assert chosen is not None
    assert str(good) in chosen


def test_it_gives_up_cleanly_when_nothing_is_writable(tmp_path, monkeypatch):
    """No usable directory is not a crash here.

    config.py's own refusal is the loud one, and it names the path and
    the reason. Swallowing it here would replace a clear message with
    silence.
    """
    blocked = tmp_path / "data"
    blocked.mkdir()

    real_write_text = pathlib.Path.write_text

    def fake_write_text(self, *a, **kw):
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(pathlib.Path, "write_text", fake_write_text)
    monkeypatch.setattr(_android_paths, "writable_dirs",
                        lambda: [str(blocked)])
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    assert _android_paths.point_config_at_writable_storage() is None


def test_is_android_is_false_off_android():
    """The fix must not move a Linux user's config dir."""
    assert _android_paths.is_android() is False
    assert not hasattr(sys, "getandroidapilevel")

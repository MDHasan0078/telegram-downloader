"""The Android bridge: the functions Kotlin calls.

Everything here runs in a SUBPROCESS on purpose. `bridge.init()` sets
`HOME` and `XDG_CONFIG_HOME` and then imports `telegram_downloader.config`,
which computes CONFIG_DIR at IMPORT time -- so the bridge can only be
tested in a fresh interpreter, and running it in-process would leak that
environment into every other test in the session.

The subprocess prints one JSON object; the assertions are on that.
"""
import json
import os
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = REPO / "src"

SCRIPT = r'''
import json, os, sys
sys.path.insert(0, sys.argv[2])
from telegram_downloader import bridge

tmp = sys.argv[1]
out = {}

# --- init(): the app's own directories --------------------------------------
out["init"] = bridge.init(os.path.join(tmp, "home"),
                           os.path.join(tmp, "downloads"),
                           os.path.join(tmp, "cache"))
out["config_dir_mode"] = oct(os.stat(out["init"]["config_dir"]).st_mode & 0o777)
out["xdg_mode"] = oct(os.stat(os.path.join(tmp, "home", "xdg")).st_mode & 0o777)
out["home"] = os.environ["HOME"]
out["xdg"] = os.environ["XDG_CONFIG_HOME"]

# --- settings ---------------------------------------------------------------
out["settings"] = bridge.get_settings()
bridge.set_api_credentials("12345", "deadbeef")
out["settings_after"] = bridge.get_settings()
out["config_json_mode"] = oct(os.stat(
    os.path.join(out["init"]["config_dir"], "config.json")).st_mode & 0o777)

# --- links ------------------------------------------------------------------
out["links"] = bridge.parse_links(
    "https://t.me/chan/1\nhttps://t.me/chan/2\nnot a link\n")
out["links_dedup"] = bridge.parse_links("https://t.me/chan/1 https://t.me/chan/1")

# --- the queue --------------------------------------------------------------
# The worker is stubbed out: it would open a network connection, and this
# test is about the bridge, not about Telegram.
bridge._engine.start_queue_async = lambda: None
out["add"] = bridge.add_links(["https://t.me/chan/1", "https://t.me/chan/2"])
out["queue"] = bridge.get_queue_items()
out["retry"] = bridge.retry_item(out["queue"][0]["id"])
out["remove"] = bridge.remove_item(out["queue"][0]["id"])
out["queue_after_remove"] = bridge.get_queue_items()
out["clear"] = bridge.clear_queue()
out["queue_after_clear"] = bridge.get_queue_items()

# --- credentials are required before anything is queued ---------------------
bridge.reset_everything()
out["add_without_api"] = bridge.add_links(["https://t.me/chan/1"])

# --- the download folder ----------------------------------------------------
out["dir_ok"] = bridge.set_download_dir(os.path.join(tmp, "other"))
out["dir_bad"] = bridge.set_download_dir("/")
out["dir_after"] = bridge.get_download_dir()

# --- the login handshake ----------------------------------------------------
# These must return an ERROR DICT, not raise. They used to raise
# UnboundLocalError: the inner coroutine did `phone = phone.strip()`, and
# the right-hand side read the local variable it was about to create.
out["login_begin_empty"] = bridge.login_begin("")
out["login_code_empty"] = bridge.login_code("")
out["login_password_empty"] = bridge.login_password("")

# --- dependencies and capabilities ------------------------------------------
out["deps"] = bridge.get_dependencies()
out["caps"] = bridge.get_capabilities()

# --- the update download ----------------------------------------------------
# The network is stubbed out: this is about the bridge's own contract --
# which asset it picks, where the file lands, and what the state says
# while it is running -- not about GitHub.
from telegram_downloader import updates as _updates

class _Asset:
    def __init__(self, name):
        self.name = name
        self.url = "https://example.invalid/releases/" + name

class _Info:
    latest_version = "9.9.9"
    release_url = "https://example.invalid/releases"
    assets = [
        _Asset("telegram-downloader_9.9.9.apk"),
        _Asset("telegram-downloader_9.9.9_amd64.deb"),
        _Asset("telegram-downloader_9.9.9.dmg"),
    ]
    def is_newer_than(self, current):
        return True
    def asset_for_platform(self, operating_system=None):
        wanted = {
            "android": "telegram-downloader_9.9.9.apk",
            "linux": "telegram-downloader_9.9.9_amd64.deb",
        }.get(operating_system)
        for asset in self.assets:
            if asset.name == wanted:
                return asset
        return None

_updates.check_for_update = lambda *a, **k: _Info()

_seen = {}

def _fake_download(asset, dest_dir, *args, **kwargs):
    _seen["asset"] = asset.name
    _seen["dest_dir"] = str(dest_dir)
    _seen["tag_version"] = args[0] if args else kwargs.get("tag_version")
    progress = kwargs.get("progress")
    if progress is not None:
        progress(512, 1024)
        progress(1024, 1024)
    return os.path.join(str(dest_dir), asset.name)

_updates.download_asset = _fake_download

out["update_state_before"] = bridge.get_update_download_state()
out["update_start"] = bridge.start_update_download()

# The download runs in the background: the bridge returns as soon as it
# is under way, and the UI polls the state. Wait for it to finish.
import time
_state = bridge.get_update_download_state()
_deadline = time.time() + 30
while time.time() < _deadline:
    _state = bridge.get_update_download_state()
    if _state["status"] in ("done", "error"):
        break
    time.sleep(0.05)
out["update_state_after"] = _state
out["update_seen"] = _seen

print(json.dumps(out))
'''


def _run(tmp_path):
    env = dict(os.environ)
    env.pop("XDG_CONFIG_HOME", None)
    env.pop("TG_API_ID", None)
    env.pop("TG_API_HASH", None)
    env.pop("TG_DOWNLOAD_DIR", None)
    env["HOME"] = str(tmp_path / "home")
    env["PYTHONPATH"] = str(SRC)
    proc = subprocess.run([sys.executable, "-c", SCRIPT, str(tmp_path),
                           str(SRC)],
                          env=env, capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, f"bridge script failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_init_uses_the_apps_own_private_config_dir(tmp_path):
    data = _run(tmp_path)
    # The config dir lives under the app's own directory, NOT under
    # ~/.config of the desktop layout.
    assert data["init"]["config_dir"].startswith(str(tmp_path / "home"))
    assert "telegram-downloader" in data["init"]["config_dir"]
    # And it satisfies the checks config.py enforces: owner-only.
    assert data["config_dir_mode"] == "0o700"
    assert data["xdg_mode"] == "0o700"
    assert data["home"] == str(tmp_path / "home")
    assert data["xdg"] == str(tmp_path / "home" / "xdg")


def test_settings_roundtrip_and_are_private(tmp_path):
    data = _run(tmp_path)
    assert data["settings"]["has_api"] is False
    assert data["settings_after"]["api_id"] == "12345"
    assert data["settings_after"]["api_hash"] == "deadbeef"
    assert data["settings_after"]["has_api"] is True
    # The credentials are a secret: config.json stays owner-only.
    assert data["config_json_mode"] == "0o600"


def test_split_urls_uses_the_engine_helper(tmp_path):
    data = _run(tmp_path)
    assert data["links"] == ["https://t.me/chan/1", "https://t.me/chan/2"]
    assert data["links_dedup"] == ["https://t.me/chan/1"]


def test_add_links_queues_and_reports(tmp_path):
    data = _run(tmp_path)
    assert data["add"]["ok"] is True
    assert data["add"]["added"] == 2
    assert len(data["queue"]) == 2
    assert data["queue"][0]["url"] == "https://t.me/chan/1"
    assert data["queue"][0]["status"] == "queued"
    assert data["queue"][0]["ui_status"] == "waiting"
    assert data["remove"] is True
    assert len(data["queue_after_remove"]) == 1
    assert data["clear"] is True
    assert data["queue_after_clear"] == []


def test_add_links_without_credentials_is_refused(tmp_path):
    data = _run(tmp_path)
    assert data["add_without_api"]["ok"] is False
    assert "Account auth" in data["add_without_api"]["error"]


def test_download_folder_is_validated_not_trusted(tmp_path):
    data = _run(tmp_path)
    assert data["dir_ok"]["ok"] is True
    # The filesystem root is refused by the engine's own check, so the app
    # can never show a folder that download time would refuse.
    assert data["dir_bad"]["ok"] is False
    assert data["dir_after"]["download_dir"] == str(tmp_path / "other")


def test_login_handshake_reports_errors_instead_of_raising(tmp_path):
    """The login steps must return an error dict, never raise.

    They used to raise UnboundLocalError: the inner coroutine assigned
    `phone = phone.strip()`, and the right-hand side then read the local
    variable that did not exist yet -- so the FIRST tap on "Log in" would
    have taken the app down.
    """
    data = _run(tmp_path)
    for key in ("login_begin_empty", "login_code_empty",
                "login_password_empty"):
        step = data[key]
        assert step["ok"] is False, key
        assert step["state"] == "error", key
        assert step["error"], key


def test_dependencies_say_the_truth_about_ffmpeg(tmp_path):
    data = _run(tmp_path)
    names = [d["name"] for d in data["deps"]]
    assert any("ffmpeg" in n for n in names)
    ff = next(d for d in data["deps"] if "ffmpeg" in d["name"])
    # Android has no ffmpeg and no way to install one: the app must say so
    # plainly instead of failing at the end of a long download.
    assert "Android" in ff["hint"]
    assert "install" in ff["install"].lower() or ff["install"]
    caps = data["caps"]
    assert caps["platform"] == "android"
    assert caps["modes"] == (["1", "2", "3"] if caps["ffmpeg"] else ["1"])


def test_the_update_download_picks_the_android_package(tmp_path):
    """On Android the app must fetch the .apk, not the desktop .deb.

    platform.system() reports "Linux" on Android, so the asset has to be
    resolved with the platform stated. Without that the app would look
    for the .deb, not find one, and the updater would say "no installer
    for this platform" on the one platform that CAN install one.
    """
    data = _run(tmp_path)
    assert data["update_start"]["ok"] is True
    assert data["update_seen"]["asset"] == "telegram-downloader_9.9.9.apk"
    # The release version travels with it: the download is verified
    # against the signed manifest of that exact release.
    assert data["update_seen"]["tag_version"] == "9.9.9"


def test_the_update_download_lands_in_the_apps_own_storage(tmp_path):
    """The package goes to the app's own directory, not the download folder.

    Two reasons, both load-bearing: the app has to be able to hand the
    file to the system installer afterwards, and a 25 MB package the
    user never asked for does not belong in their Downloads folder.
    """
    data = _run(tmp_path)
    dest = data["update_seen"]["dest_dir"]
    assert dest.startswith(str(tmp_path / "home"))
    assert dest.endswith(os.path.join("home", "updates"))


def test_the_update_download_reports_progress_and_completes(tmp_path):
    """The state the UI polls has to say what is happening.

    This is the whole bug: the updater looked like it had done nothing,
    because nothing said that a download was under way.
    """
    data = _run(tmp_path)
    before = data["update_state_before"]
    assert before["status"] == "idle"
    assert before["progress"] == 0.0

    after = data["update_state_after"]
    assert after["status"] == "done", after
    assert after["progress"] == 1.0
    assert after["error"] == ""
    # The bytes, and the human line under the bar.
    assert after["done"] == 1024
    assert after["total"] == 1024
    assert after["detail"]
    # And where the verified package ended up.
    assert after["path"].endswith("telegram-downloader_9.9.9.apk")
    assert after["version"] == "9.9.9"
    assert after["name"] == "telegram-downloader_9.9.9.apk"


def test_the_update_state_has_every_field_the_kotlin_reads(tmp_path):
    """The Kotlin reads these keys by name, through a JSON object.

    A missing key is not a compile error on either side -- it is a
    blank line on the screen -- so the shape is checked here.
    """
    data = _run(tmp_path)
    for key in ("status", "progress", "done", "total", "detail",
                "error", "path", "version", "name"):
        assert key in data["update_state_after"], (
            f"the update state has no {key!r}, and the Kotlin reads it")

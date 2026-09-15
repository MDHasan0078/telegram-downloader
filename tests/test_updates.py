"""Update-checker tests (no network — urlopen is stubbed)."""
import io

import telegram_downloader.updates as upd


def _info():
    return upd.UpdateInfo(
        latest_version="0.2.0",
        release_url="https://github.com/x/y/releases/tag/v0.2.0",
        assets=[
            upd.UpdateAsset("telegram-downloader_0.2.0_amd64.deb", "http://e/d.deb"),
            upd.UpdateAsset("telegram-downloader_0.2.0.apk", "http://e/a.apk"),
            upd.UpdateAsset("telegram-downloader_0.2.0.dmg", "http://e/m.dmg"),
        ],
    )


def test_is_newer():
    info = _info()
    assert info.is_newer_than("0.1.0")
    assert info.is_newer_than("0.1.9")
    assert not info.is_newer_than("0.2.0")
    assert not info.is_newer_than("0.2.1")
    assert not info.is_newer_than("1.0.0")
    assert info.is_newer_than("v0.1.0")  # tolerates v-prefix


def test_asset_for_platform_versioned():
    info = _info()
    assert info.asset_for_platform("linux").name.endswith(".deb")
    assert info.asset_for_platform("android").name.endswith(".apk")
    assert info.asset_for_platform("darwin").name.endswith(".dmg")
    assert info.asset_for_platform("plan9") is None


def test_asset_fallback_to_suffix():
    # Security: no suffix fallback — an unrelated *.deb must NOT be
    # presented as a trusted update (exact versioned name only).
    info = upd.UpdateInfo("9.9.9", "http://e", [upd.UpdateAsset("legacy_all.deb", "http://e/x")])
    assert info.asset_for_platform("linux") is None


def test_check_offline_returns_none(monkeypatch):
    def boom(*a, **k):
        raise OSError("offline")
    monkeypatch.setattr(upd, "_urlopen_no_redirect", boom)
    assert upd.check_for_update() is None


def test_check_parses_release(monkeypatch):
    payload = b'''{"tag_name": "v0.3.0",
        "html_url": "https://github.com/MDHasan0078/telegram-downloader/releases/tag/v0.3.0",
        "assets": [{"name": "telegram-downloader_0.3.0_amd64.deb",
                    "browser_download_url": "https://github.com/MDHasan0078/telegram-downloader/releases/download/v0.3.0/telegram-downloader_0.3.0_amd64.deb"}]}'''

    class FakeResp:
        status = 200
        def read(self):
            return payload
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(upd, "_urlopen_no_redirect", lambda *a, **k: FakeResp())
    info = upd.check_for_update()
    assert info and info.latest_version == "0.3.0"
    assert info.is_newer_than("0.1.0")
    assert info.asset_for_platform("linux").url.startswith("https://github.com/")

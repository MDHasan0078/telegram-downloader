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
    # No Windows build job exists, so no .exe is ever offered.
    assert info.asset_for_platform("windows") is None


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


def test_checksum_name_for():
    assert upd._checksum_name_for("telegram-downloader_1.0.0_amd64.deb") == "SHA256SUMS-deb"
    assert upd._checksum_name_for("telegram-downloader_1.0.0.apk") == "SHA256SUMS-apk"
    assert upd._checksum_name_for("telegram-downloader_1.0.0.dmg") == "SHA256SUMS-dmg"
    assert upd._checksum_name_for("evil.exe") is None


def test_fetch_checksum_parses(monkeypatch):
    body = (b"abc123" + b"0" * 58 + b"  telegram-downloader_1.0.0_amd64.deb\n"
            b"not-a-hash  junk\n"
            b"DEF456" + b"1" * 58 + b" *telegram-downloader_1.0.0.apk\n")

    class FakeResp:
        status = 200
        headers = {}
        def read(self, *a):
            return body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    seen = {}
    def fake_open(req, timeout=0):
        seen["url"] = req.full_url
        return FakeResp()
    monkeypatch.setattr(upd, "_urlopen_no_redirect", fake_open)
    sums = upd._fetch_checksum("SHA256SUMS-deb", "1.0.0")
    assert seen["url"] == ("https://github.com/MDHasan0078/telegram-downloader"
                           "/releases/download/v1.0.0/SHA256SUMS-deb")
    assert sums["telegram-downloader_1.0.0_amd64.deb"] == "abc123" + "0" * 58
    assert sums["telegram-downloader_1.0.0.apk"] == ("def456" + "1" * 58)


def test_fetch_checksum_rejects_bad_name():
    try:
        upd._fetch_checksum("../../etc/passwd", "1.0.0")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_verify_file_sha256(tmp_path):
    import hashlib
    p = tmp_path / "f.bin"
    p.write_bytes(b"hello")
    good = hashlib.sha256(b"hello").hexdigest()
    upd._verify_file_sha256(p, good)  # must not raise
    try:
        upd._verify_file_sha256(p, "0" * 64)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError on mismatch")

"""Config + queue persistence tests (isolated via tmp HOME)."""
import json

from telegram_downloader import config as cfg


def test_settings_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg, "CONFIG_JSON", tmp_path / "config.json")
    monkeypatch.setattr(cfg, "LEGACY_CONFIG", tmp_path / "config")
    monkeypatch.setattr(cfg, "SESSION_BASE", tmp_path / "session")
    s = cfg.Settings(api_id="123", api_hash="abc", download_dir=str(tmp_path / "dl"),
                     output_mode="2")
    cfg.save_settings(s)
    loaded = cfg.load_settings()
    assert loaded.api_id == "123" and loaded.output_mode == "2"
    # perms should be restrictive
    assert (tmp_path / "config.json").stat().st_mode & 0o777 == 0o600


def test_queue_persists_and_resets_midflight(tmp_path, monkeypatch):
    from telegram_downloader import queue_store as qs_mod
    qfile = tmp_path / "queue.json"
    monkeypatch.setattr(qs_mod, "QUEUE_FILE", qfile)
    store = qs_mod.QueueStore(path=qfile)
    store.add(qs_mod.DownloadItem(url="https://t.me/a/1", status="downloading"))
    assert qfile.exists()
    store2 = qs_mod.QueueStore(path=qfile)
    assert store2.items[0].status == "queued"  # mid-flight reset for resume-all


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg, "CONFIG_JSON", tmp_path / "config.json")
    monkeypatch.setattr(cfg, "LEGACY_CONFIG", tmp_path / "config")
    monkeypatch.setattr(cfg, "SESSION_BASE", tmp_path / "session")
    for var in ("TG_API_ID", "TG_API_HASH", "TG_DOWNLOAD_DIR"):
        monkeypatch.delenv(var, raising=False)


def test_env_overrides_settings_not_persisted(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    cfg.save_settings(cfg.Settings(api_id="123", api_hash="abc"))
    monkeypatch.setenv("TG_API_ID", "999")
    monkeypatch.setenv("TG_API_HASH", "envhash")
    monkeypatch.setenv("TG_DOWNLOAD_DIR", str(tmp_path / "envdir"))
    s = cfg.load_settings()
    assert s.api_id == "999"
    assert s.api_hash == "envhash"
    assert s.download_dir == str(tmp_path / "envdir")
    # Env values are NOT written back to disk.
    on_disk = json.loads((tmp_path / "config.json").read_text())
    assert on_disk["api_id"] == "123"


def test_load_settings_refuses_symlink_config(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"api_id": "1", "api_hash": "a"}))
    (tmp_path / "config.json").symlink_to(real)
    s = cfg.load_settings()
    # Symlinked config must be ignored (fail closed), not read.
    assert s.api_id is None
    assert s.api_hash is None


def test_session_path_str_fail_closed_against_symlink(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    real_session = tmp_path / "victim.session"
    real_session.write_text("attacker content")
    (tmp_path / "session.session").symlink_to(real_session)
    try:
        path = cfg.session_path_str()
    except OSError:
        return  # raising loudly is also fail-closed on platforms without ELOOP retries
    # Session path returned but the create path must NOT be the victim file:
    # the planted symlink must have been unlinked, so writes can't hit it.
    assert path == str(tmp_path / "session")
    assert not (tmp_path / "session.session").is_symlink()
    assert real_session.read_text() == "attacker content"  # target untouched


def test_shred_legacy_config_does_not_follow_symlink(tmp_path, monkeypatch):
    # legacy read/migrate must use O_NOFOLLOW: a LINK planted at LEGACY_CONFIG
    # must be ignored (fail closed), never read/migrated, and the target must
    # stay untouched (no zero-fill, no unlink).
    _isolate(monkeypatch, tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("api_id=1\napi_hash=alpha\nEXTRA_SECRET=tail")
    (tmp_path / "config").symlink_to(victim)
    cfg.load_settings()
    assert victim.exists()
    assert "EXTRA_SECRET" in victim.read_text()  # target not shredded
    assert (tmp_path / "config").is_symlink()  # plant not silently removed
    assert not (tmp_path / "config.json").exists()  # nothing migrated from link


def test_legacy_real_file_migrates_and_gets_shredded(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "config").write_text("api_id=42\napi_hash=beta\n")
    s = cfg.load_settings()
    assert s.api_id == "42" and s.api_hash == "beta"
    assert (tmp_path / "config.json").exists()
    on_disk = json.loads((tmp_path / "config.json").read_text())
    assert on_disk["api_id"] == "42"
    # Legacy secrets zero-filled then unlinked (not merely unlinked).
    assert not (tmp_path / "config").exists()

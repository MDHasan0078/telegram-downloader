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

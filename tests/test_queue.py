"""Queue + constants tests (no network, no GUI)."""
import json

import telegram_downloader.queue_store as qs
from telegram_downloader.constants import (
    MAX_BATCH_BYTES,
    MAX_FILE_BYTES,
    MAX_URL_LEN,
    MAX_URL_LIST_BYTES,
    MAX_URLS,
)
from telegram_downloader.queue_store import DownloadItem, QueueStore


def test_sanitize_item_clamps_all_fields():
    it = DownloadItem(url="u" * 900, title="t" * 900, error="e" * 900,
                      dest="d" * 900, ext=".exe\"bad", size=10 ** 12,
                      progress=999.0, status="bogus", id="x" * 100)
    out = qs._sanitize_item(it)
    assert len(out.url) == qs.MAX_STR_FIELD
    assert len(out.title) == qs.MAX_STR_FIELD
    assert out.ext == ".mp4"
    assert out.size == qs.MAX_NUM_FIELD
    assert out.progress == 100.0
    assert out.status == "queued"
    assert len(out.id) <= 64


def test_sanitize_item_strips_control_chars():
    it = DownloadItem(title="a\x00b\x1bc", url="http://x/\n")
    out = qs._sanitize_item(it)
    assert "\x00" not in out.title and "\n" not in out.url


def test_add_extend_update_share_validator(tmp_path, monkeypatch):
    monkeypatch.setattr(qs, "QUEUE_FILE", tmp_path / "q.json")
    store = QueueStore(path=tmp_path / "q.json")
    evil = DownloadItem(title="t" * 900, status="nope", size=-5)
    store.add(evil)
    assert len(store.items[0].title) == qs.MAX_STR_FIELD
    assert store.items[0].status == "queued"
    assert store.items[0].size == 0
    store.extend([DownloadItem(title="ok"), DownloadItem(ext=".bad!ext")])
    assert store.items[2].ext == ".mp4"
    # update keeps skip-on-invalid for enums
    store.update(store.items[0].id, status="bogus", title="new")
    assert store.items[0].status == "queued"
    assert store.items[0].title == "new"


def test_load_refuses_symlink(tmp_path):
    real = tmp_path / "real.json"
    real.write_text(json.dumps([{"url": "http://x", "title": "t"}]))
    link = tmp_path / "q.json"
    link.symlink_to(real)
    store = QueueStore(path=link)
    assert store.items == []  # refused, not followed


def test_load_enforces_caps(tmp_path):
    big = [{"url": "http://x/%d" % i, "title": "t"} for i in range(qs.MAX_QUEUE_ITEMS + 10)]
    p = tmp_path / "q.json"
    p.write_text(json.dumps(big))
    store = QueueStore(path=p)
    assert store.items == []  # over-cap file rejected, backed up
    assert list(tmp_path.glob("q.json.corrupt-*.bak"))


def test_constants_sane():
    assert MAX_URLS == 100
    assert MAX_BATCH_BYTES == 256 * 1024
    assert MAX_URL_LEN == 2000
    assert MAX_URL_LIST_BYTES == 100_000
    assert MAX_FILE_BYTES == 8 * 1024 ** 3

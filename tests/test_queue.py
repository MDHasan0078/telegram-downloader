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


def test_dunder_keys_never_reflected(tmp_path):
    evil = {"__class__": {"__init__": "__globals__"},
            "__dict__": {"payload": 1}, "url": "http://x", "title": "ok"}
    it = DownloadItem.from_dict(evil)
    assert it.title == "ok"
    assert not hasattr(it, "__globals__")
    p = tmp_path / "q.json"
    p.write_text(json.dumps([{"__class__": {"x": 1}, "url": "http://x", "title": "t"}]))
    store = QueueStore(path=p)
    assert len(store.items) == 1
    assert store.items[0].__class__ is DownloadItem
    store.update(store.items[0].id, **{"__init__": "__globals__", "title": "t2"})
    assert store.items[0].title == "t2"
    assert not hasattr(type(store.items[0]), "__globals__")


def test_mode_field_default_and_sanitize(tmp_path):
    it = DownloadItem.from_dict({"url": "http://x", "mode": "bogus"})
    assert it.mode == "1"
    it2 = DownloadItem.from_dict({"url": "http://x", "mode": "3"})
    assert it2.mode == "3"
    store = QueueStore(path=tmp_path / "q.json")
    store.add(DownloadItem(url="http://x", mode="z"))
    assert store.items[0].mode == "1"
    store.update(store.items[0].id, mode="2")
    assert store.items[0].mode == "2"
    store.update(store.items[0].id, mode="99")  # invalid stays
    assert store.items[0].mode == "2"


def test_extend_returns_only_added(tmp_path):
    store = QueueStore(path=tmp_path / "q.json")
    for i in range(qs.MAX_QUEUE_ITEMS):
        store.add(DownloadItem(url=f"http://x/{i}"))
    added = store.extend([DownloadItem(url="http://a"), DownloadItem(url="http://b")])
    assert added == []
    store2 = QueueStore(path=tmp_path / "q2.json")
    got = store2.extend([DownloadItem(url="http://a"), DownloadItem(url="http://b")])
    assert [i.url for i in got] == ["http://a", "http://b"]
    assert len(store2.items) == 2


def test_clear_all(tmp_path):
    store = QueueStore(path=tmp_path / "q.json")
    store.add(DownloadItem(url="http://x"))
    store.clear_all()
    assert store.items == []
    assert json.loads(store.path.read_text()) == []

def test_midflight_statuses_back_to_queued(tmp_path):
    p = tmp_path / "q.json"
    p.write_text(json.dumps([{"url": "http://x", "title": "t", "status": "downloading"},
                             {"url": "http://y", "title": "u", "status": "done"}]))
    store = QueueStore(path=p)
    assert store.items[0].status == "queued"
    assert store.items[1].status == "done"


def test_method_shadow_ignored_on_load_and_save(tmp_path):
    # A crafted file with {"to_dict": "boom"} used to shadow the dataclass
    # method and crash the next save() with "str not callable".
    p = tmp_path / "q.json"
    p.write_text(json.dumps([
        {"url": "http://x", "title": "real", "to_dict": "boom", "from_dict": "boom"},
    ]))
    store = QueueStore(path=p)
    assert len(store.items) == 1
    assert store.items[0].title == "real"
    store.save()  # must not raise
    data = json.loads(p.read_text())
    assert data[0]["url"] == "http://x"
    assert "to_dict" not in data[0]
    # update() with a method name as key must be a no-op too.
    store.update(store.items[0].id, to_dict="boom", title="t2")
    assert store.items[0].title == "t2"
    store.save()


def test_update_rejects_empty_or_nonstring_id(tmp_path):
    store = QueueStore(path=tmp_path / "q.json")
    it = DownloadItem(url="http://x")
    store.add(it)
    before = it.id
    store.update(it.id, id="")
    assert it.id == before  # empty id must not be applied
    store.update(it.id, id=12345)
    assert it.id == before  # non-string id must not be applied


def test_ext_to_nonstring_coerced_to_mp4(tmp_path):
    # A crafted int/float ext must not crash re.fullmatch on load.
    p = tmp_path / "q.json"
    p.write_text(json.dumps([{"url": "http://x", "title": "t", "ext": 123}]))
    store = QueueStore(path=p)
    assert store.items[0].ext == ".mp4"
    it = DownloadItem.from_dict({"url": "http://x", "ext": 12.5})
    assert it.ext == ".mp4"


def test_added_at_nonfinite_and_negative_clamped(tmp_path):
    it = DownloadItem.from_dict({"url": "http://x", "added_at": float("nan")})
    assert it.added_at == 0.0
    it2 = DownloadItem.from_dict({"url": "http://x", "added_at": -50.0})
    assert it2.added_at == 0.0
    # Non-numeric added_at from a corrupt file also lands at 0.
    p = tmp_path / "q.json"
    p.write_text(json.dumps([{"url": "http://x", "title": "t", "added_at": "abc"}]))
    store = QueueStore(path=p)
    assert store.items[0].added_at == 0.0


def test_load_nonfinite_number_backs_up(tmp_path):
    # JSON NaN/Infinity must be rejected at parse time (parse_constant),
    # not persisted as floats that poison comparisons/formatting.
    p = tmp_path / "q.json"
    p.write_text('[{"url": "http://x", "title": "t", "added_at": NaN}]')
    store = QueueStore(path=p)
    assert store.items == []
    assert list(tmp_path.glob("q.json.corrupt-*.bak"))

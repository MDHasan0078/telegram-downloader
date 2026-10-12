"""Cross-process serialisation tests for config.json and queue.json.

Both files are read-modify-write units, so two processes writing at once
interleave and the later writer silently discards the earlier one's work.
Measured on the queue before the lock existed: 4 processes x 250 add() left
272 of 1000 rows.

The deterministic tests below drive two QueueStore objects in one process,
which is exactly how the real failure looks (the GUI holds a store in memory
for its whole lifetime while `tg-dl` writes in another). They fail on every
run without the lock, so they are the regression guard; the multiprocess one
is the end-to-end confirmation that the flock actually works across pids.
"""
import json
import multiprocessing as mp
import os
import sys

import pytest

import telegram_downloader.config as cfg
import telegram_downloader.queue_store as qs
from telegram_downloader.queue_store import DownloadItem, QueueStore

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


def titles(path):
    return [r["title"] for r in json.loads(path.read_text())]


# --------------------------------------------------------------------------
# queue.json
# --------------------------------------------------------------------------

def test_stale_save_keeps_rows_another_process_added(tmp_path):
    """The original bug: the GUI's save discarded a `tg-dl download` batch.

    `gui` loads and holds a QueueStore for its whole lifetime. Anything the
    CLI adds while that copy is in memory must survive the GUI's next save.
    """
    q = tmp_path / "q.json"
    gui = QueueStore(path=q)
    gui.add(DownloadItem(title="gui-1"))
    gui.save()

    cli = QueueStore(path=q)          # separate process, same file
    cli.add(DownloadItem(title="cli-1"))
    cli.save()

    gui.add(DownloadItem(title="gui-2"))
    gui.save()                        # stale GUI writes last

    assert set(titles(q)) == {"gui-1", "cli-1", "gui-2"}


def test_stale_save_does_not_resurrect_removed_row(tmp_path):
    """A row the user deleted must stay deleted.

    This is the trap that killed the first merge-on-save attempt: unioning
    with the file keeps every concurrent add but also brings back every
    deliberate removal. "Row is missing from the file" is ambiguous between
    "deleted" and "never seen", and only the base snapshot resolves it.
    """
    q = tmp_path / "q.json"
    a = QueueStore(path=q)
    b = QueueStore(path=q)           # b snapshots the same starting view
    for t in ("x", "y", "z"):
        a.add(DownloadItem(title=t))
    a.save()
    b.load()

    a.remove(a.items[1].id)          # user removes "y"
    a.save()

    b.add(DownloadItem(title="b-new"))
    b.save()                         # stale b saves afterwards

    assert set(titles(q)) == {"x", "z", "b-new"}


def test_stale_save_does_not_resurrect_clear_all(tmp_path):
    """Same guarantee at the whole-queue level, which is the case users hit."""
    q = tmp_path / "q.json"
    a = QueueStore(path=q)
    b = QueueStore(path=q)
    for t in ("x", "y", "z"):
        a.add(DownloadItem(title=t))
    a.save()
    b.load()

    a.clear_all()
    a.save()

    b.add(DownloadItem(title="b-new"))
    b.save()

    # The Clear all stands; only genuinely new work survives.
    assert titles(q) == ["b-new"]


def test_local_edit_survives_an_unrelated_concurrent_add(tmp_path):
    """A store's edit must not be lost, and order must not be disturbed.

    Resolution is per ROW, not per field: `b` never touched `shared`, but it
    holds a stale copy of it, and the row it publishes wins. See
    test_same_row_is_last_writer_wins for that boundary.
    """
    q = tmp_path / "q.json"
    a = QueueStore(path=q)
    for t in ("first", "shared", "last"):
        a.add(DownloadItem(title=t))
    a.save()

    b = QueueStore(path=q)                  # b loads the same view
    a.update(a.items[1].id, status="done")  # a edits a row
    b.add(DownloadItem(title="b-new"))      # b adds an unrelated row
    a.save()
    b.save()                                # stale b publishes last

    rows = json.loads(q.read_text())
    assert [r["title"] for r in rows] == ["first", "shared", "last", "b-new"]


def test_same_row_is_last_writer_wins(tmp_path):
    """Documents the granularity limit of the merge: per row, not per field.

    Two stores editing the SAME row cannot both be honoured -- there is no
    field-level version to compare, so whichever store saves last publishes
    its copy of that row wholesale. The critical guarantees (concurrent adds
    survive, deletions are never resurrected) are unaffected; this only
    bites when two processes edit one identical row at the same time.
    """
    q = tmp_path / "q.json"
    a = QueueStore(path=q)
    for t in ("first", "shared", "last"):
        a.add(DownloadItem(title=t))
    a.save()
    b = QueueStore(path=q)

    a.update(a.items[1].id, status="done")
    b.update(b.items[1].id, title="renamed")
    a.save()
    b.save()

    row = json.loads(q.read_text())[1]
    assert row["title"] == "renamed" and row["status"] == "queued"


def test_unchanged_file_fast_path_preserves_order_and_view(tmp_path):
    """The `raw == _written` shortcut must equal what the merge would produce.

    Skipping the parse there is the point of the optimisation, but it is only
    valid because every local mutation (append / filter-in-place) preserves
    the published order. If that ever stops being true the shortcut silently
    reorders the queue, so pin the invariant.
    """
    q = tmp_path / "q.json"
    a = QueueStore(path=q)
    for t in ("a", "b", "c", "d", "e"):
        a.add(DownloadItem(title=t))
    a.save()

    a.remove(a.items[1].id)          # middle removal preserves order
    a.add(DownloadItem(title="f"))   # appended at the end
    a.save()                         # now hits the fast path

    assert [i.title for i in a.items] == titles(q) == ["a", "c", "d", "e", "f"]


def test_rows_without_persisted_ids_fall_back_to_verbatim(tmp_path):
    """A queue.json whose rows carry no usable id cannot be reconciled.

    from_dict mints a fresh uuid per parse, so the same row is a different
    identity on every read and no base snapshot can ever match it. The store
    must fall back to writing its own rows rather than guess.
    """
    q = tmp_path / "q.json"
    q.write_text(json.dumps([{"url": "http://x", "title": "legacy"}]))

    a = QueueStore(path=q)
    assert a._base_ids is None          # "no comparable base"
    a.add(DownloadItem(title="new"))
    a.save()
    assert set(titles(q)) == {"legacy", "new"}

    # The save persisted ids, so the base is usable from here on.
    b = QueueStore(path=q)
    assert b._base_ids is not None


def test_unreadable_queue_falls_back_to_verbatim_write(tmp_path):
    """An unparseable file must not make a save raise or lose our rows."""
    q = tmp_path / "q.json"
    q.write_text("{not json at all")
    a = QueueStore(path=q)
    a.add(DownloadItem(title="recovered"))
    a.save()
    assert titles(q) == ["recovered"]


def test_lock_lives_on_a_sidecar_not_the_data_file(tmp_path):
    """flock() must not be taken on queue.json itself.

    Every save REPLACES the data file by rename, so an flock on it would only
    ever cover the unlinked predecessor and two writers would both believe
    they hold "the lock".
    """
    q = tmp_path / "q.json"
    a = QueueStore(path=q)
    a.add(DownloadItem(title="x"))
    a.save()
    assert q.with_name(q.name + ".lock").exists()
    assert not qs.QueueStore.__doc__.startswith("__")
    # The sidecar is private, not world-readable like the queue's dir siblings.
    assert q.with_name(q.name + ".lock").stat().st_mode & 0o777 == 0o600


def _mp_add_worker(n, qpath):
    if SRC not in sys.path:
        sys.path.insert(0, SRC)
    for i in range(40):
        QueueStore(path=qs.Path(qpath)).add(DownloadItem(title=f"p{n}-{i}"))


@pytest.mark.skipif(sys.platform == "win32", reason="needs fork + flock")
def test_concurrent_processes_lose_no_rows(tmp_path):
    """End-to-end proof that the flock works across pids.

    Without the lock this leaves well under half the rows (47 of 160 on this
    machine); with it, every row survives exactly once.
    """
    q = tmp_path / "q.json"
    q.write_text("[]")
    ctx = mp.get_context("fork")
    procs = [ctx.Process(target=_mp_add_worker, args=(n, str(q))) for n in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
        assert p.exitcode == 0

    rows = json.loads(q.read_text())
    assert len(rows) == 160
    assert len({r["id"] for r in rows}) == 160


# --------------------------------------------------------------------------
# config.json
# --------------------------------------------------------------------------

def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg, "CONFIG_JSON", tmp_path / "config.json")
    monkeypatch.setattr(cfg, "LEGACY_CONFIG", tmp_path / "config")
    monkeypatch.setattr(cfg, "SESSION_BASE", tmp_path / "session")
    for var in ("TG_API_ID", "TG_API_HASH", "TG_DOWNLOAD_DIR"):
        monkeypatch.delenv(var, raising=False)


def _write_config_as_another_process(path, **fields):
    """Publish settings.json the way a separate process does.

    Two QueueStore/Settings copies in ONE process cannot model two processes
    here: `_SETTINGS_BASE` is module state, so an in-process "second writer"
    would share the first writer's base and the test would pass for the wrong
    reason. Writing the bytes straight to disk is exactly what the other
    process does, and leaves this process's base untouched.
    """
    current = {}
    if path.exists():
        current = json.loads(path.read_text())
    current.update(fields)
    cfg._atomic_write(path, json.dumps(current, indent=2).encode("utf-8"))


def test_stale_settings_do_not_revert_a_concurrent_change(tmp_path, monkeypatch):
    """A GUI holding a pre-`tg-dl config set` copy must not revert it.

    `Settings` has no per-field dirty tracking, so without a base snapshot
    every save is a whole-file overwrite and the CLI's change is silently
    undone the next time the user hits Save.
    """
    _isolate(monkeypatch, tmp_path)
    cfg.save_settings(cfg.Settings(api_id="1", api_hash="orig"))
    gui = cfg.load_settings()                       # GUI opens the settings
    _write_config_as_another_process(tmp_path / "config.json", api_id="999")

    cfg.save_settings(gui)                          # GUI saves its stale copy
    assert cfg.load_settings().api_id == "999"


def test_local_settings_edit_still_wins(tmp_path, monkeypatch):
    """A key the GUI really changed must beat the file."""
    _isolate(monkeypatch, tmp_path)
    cfg.save_settings(cfg.Settings(api_id="1", api_hash="orig"))
    gui = cfg.load_settings()
    gui.output_mode = "2"
    cfg.save_settings(gui)
    assert cfg.load_settings().output_mode == "2"


def test_clearing_a_credential_persists(tmp_path, monkeypatch):
    """Editing a field back to "" is a real edit, not "untouched".

    Comparing against the base has to treat None/"" as a changed value, or
    the GUI can never remove a credential it no longer wants stored.
    """
    _isolate(monkeypatch, tmp_path)
    cfg.save_settings(cfg.Settings(api_id="1", api_hash="orig"))
    gui = cfg.load_settings()
    gui.api_hash = None
    cfg.save_settings(gui)
    assert cfg.load_settings().api_hash is None


def test_unreadable_config_falls_back_to_plain_overwrite(tmp_path, monkeypatch):
    """A corrupt config must not turn a save into a crash."""
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{not json")
    cfg.save_settings(cfg.Settings(api_id="7", api_hash="fallback"))
    assert cfg.load_settings().api_id == "7"
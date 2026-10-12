"""Download queue model + JSON persistence (resume-all across restarts)."""
from __future__ import annotations

import json
import math
import os
import re
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Optional

from .config import (QUEUE_FILE, _atomic_write, harden_private_file,
                     interprocess_lock)
from .constants import MAX_FILE_BYTES

STATUSES = ("queued", "preview", "fetching", "downloading", "converting",
            "done", "error", "cancelled")

MAX_QUEUE_ITEMS = 1000
MAX_QUEUE_BYTES = 5 * 1024 * 1024
MAX_STR_FIELD = 500
MAX_NUM_FIELD = MAX_FILE_BYTES
_MODES = ("1", "2", "3")
# Never reflect dunder keys: a crafted queue.json with "__class__" (or
# __dict__/__globals__) previously hit `setattr` and crashed the app on
# startup with an unhandled TypeError (DoS). Blocked at every entry.
_FORBIDDEN_KEYS = frozenset(k for k in dir(object))
_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,10}")


def _sanitize_str(v: Any) -> str:
    """Clamp length + strip control chars (display/log injection)."""
    s = v if isinstance(v, str) else ""
    s = re.sub(r"[\x00-\x1f\x7f]", "?", s)
    return s[:MAX_STR_FIELD]


def _sanitize_num(v: Any, is_float: bool = False) -> Any:
    try:
        n = float(v or 0) if is_float else int(v or 0)
    except (TypeError, ValueError, OverflowError):
        # OverflowError matters: int(float("inf")) raises it, and it is an
        # ArithmeticError -- NOT a ValueError -- so it used to escape and
        # crash startup from a crafted queue.json.
        return 0.0 if is_float else 0
    if is_float:
        if not math.isfinite(n):
            return 0.0
        return max(0.0, min(100.0, n))
    return max(0, min(MAX_NUM_FIELD, n))


def _sanitize_item(it: "DownloadItem") -> "DownloadItem":
    """Single validator for every entry path (add/extend/update/from_dict).

    Previously each path clamped a different subset and they drifted.
    """
    it.url = _sanitize_str(it.url)
    it.title = _sanitize_str(it.title)
    it.error = _sanitize_str(it.error)
    it.dest = _sanitize_str(it.dest)
    if not isinstance(it.ext, str):
        it.ext = ".mp4"  # a crafted int/float must not crash re.fullmatch
    elif it.ext and not _EXT_RE.fullmatch(it.ext):
        it.ext = ".mp4"
    it.size = _sanitize_num(it.size)
    it.current_bytes = _sanitize_num(it.current_bytes)
    it.total_bytes = _sanitize_num(it.total_bytes)
    it.progress = _sanitize_num(it.progress, is_float=True)
    if it.status not in STATUSES:
        it.status = "queued"
    if it.mode not in _MODES:
        it.mode = "1"
    if not isinstance(it.id, str) or not it.id or len(it.id) > 64:
        it.id = uuid.uuid4().hex[:12]
    try:
        ts = float(it.added_at)
    except (TypeError, ValueError):
        ts = 0.0
    it.added_at = ts if math.isfinite(ts) and ts >= 0 else 0.0
    return it


@dataclass
class DownloadItem:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    url: str = ""
    title: str = ""          # final stem without extension (editable)
    ext: str = ".mp4"
    mode: str = "1"          # 1=original, 2=fast, 3=max (per-item intent)
    size: int = 0
    status: str = "queued"
    progress: float = 0.0       # 0..100
    current_bytes: int = 0
    total_bytes: int = 0
    error: str = ""
    dest: str = ""              # final path once known
    added_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "DownloadItem":
        it = DownloadItem()
        if not isinstance(d, dict):
            return it
        for k, v in d.items():
            if k in _FORBIDDEN_KEYS or k.startswith("__"):
                continue  # dunder/object attrs can never be set (DoS guard)
            if k == "status" and v not in STATUSES:
                continue  # corrupt status: leave as default "queued"
            if k == "id" and (not isinstance(v, str) or len(v) > 64):
                continue  # corrupt id: keep the generated one
            if k == "ext" and isinstance(v, str):
                if v and not _EXT_RE.fullmatch(v):
                    continue
            if k == "mode" and v not in _MODES:
                continue
            if k in _ALLOWED_FIELDS and isinstance(v, (str, int, float)) and not isinstance(v, bool):
                setattr(it, k, v)
        return _sanitize_item(it)


# Only the concrete dataclass fields are ever settable. `hasattr` is NOT
# enough: a crafted file with {"to_dict": "boom"} shadows the method and
# turns the next save() into "str not callable" (persistent crash).
_ALLOWED_FIELDS = frozenset(f.name for f in fields(DownloadItem))


def _read_items_strict(path: Path) -> tuple[list[DownloadItem], bool]:
    """Parse queue.json into items, raising on anything untrustworthy.

    Returns (items, ids_are_stable). `ids_are_stable` is False when at least
    one row had no usable persisted `id`: DownloadItem.from_dict then mints a
    FRESH uuid per parse, so the same row is a different identity on every
    read and a base snapshot built from it can never be reconciled against the
    file (verified: a hand-written or pre-id queue.json re-read as a different
    id each time). Callers must treat that as "no comparable base" and fall
    back to the pre-lock overwrite rather than guess.

    The hardened read this replaces lived inline in QueueStore.load(); it is
    now shared with the save-time merge so there is exactly one copy of the
    symlink / size-cap / FIFO defences -- two copies would inevitably drift,
    which is how they got out of sync the first time.

    Raises OSError, ValueError, TypeError or RuntimeError (RecursionError);
    callers decide what that means. Deliberately does NOT back the file up or
    reset mid-flight statuses: that is load()'s policy, not the parser's.
    """
    if path.is_symlink():
        raise ValueError("refusing to load queue through symlink")
    # O_NOFOLLOW open + fstat: closes the check-then-read swap
    # window (a link planted after is_symlink() raises ELOOP).
    # The shared helper also refuses a FIFO: O_RDONLY on a named
    # pipe blocks until a writer appears, which would wedge the
    # whole app on a single planted mkfifo.
    try:
        from .config import _open_regular_readonly
        fd = _open_regular_readonly(path)
    except OSError as exc:
        import errno as _errno
        if exc.errno == _errno.ELOOP:
            raise ValueError("refusing to load queue through symlink") from exc
        raise
    try:
        # fstat of the *held* fd (not path.stat()): closes the
        # TOCTOU where a 1-byte file is swapped for a 500MB one
        # between the size check and the read.
        if os.fstat(fd).st_size > MAX_QUEUE_BYTES:
            # Close first: raising here jumped to the outer
            # handler, which never closed fd -> one leaked fd
            # per oversized load.
            os.close(fd)
            fd = -1
            raise ValueError("queue file too large")
    except OSError:
        pass
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            fd = -1
            # Non-finite literals are coerced, NOT rejected: a
            # single poisoned field used to trip this ValueError
            # and wipe *every* item in the queue. parse_constant
            # only fires for the bare tokens NaN/Infinity/
            # -Infinity, which are numbers, so 0.0 is a safe
            # stand-in -- _sanitize_num clamps it anyway.
            raw = json.load(fh, parse_constant=lambda _c: 0.0)
    finally:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass
    if not isinstance(raw, list):
        raise ValueError("queue file must contain a list")
    if len(raw) > MAX_QUEUE_ITEMS:
        raise ValueError("queue has too many items")
    # Mirror from_dict's own id gate exactly: an id it would have rejected is
    # an id we must not treat as a stable identity either.
    stable = all(isinstance(d, dict)
                 and isinstance(d.get("id"), str)
                 and 0 < len(d["id"]) <= 64
                 for d in raw)
    return [DownloadItem.from_dict(d) for d in raw], stable


def _read_queue_bytes(path: Path) -> Optional[bytes]:
    """Raw bytes of the queue file, or None when it cannot be read.

    Bytes, not rows, on purpose: the save-time merge needs the parsed rows
    ONLY when the file is no longer what this store last published, and
    parsing is the expensive half of a save (measured over the 1000 saves of
    tests/test_queue.py::test_extend_returns_only_added: 13.4s to parse vs
    7.2s to serialise and 0.2s to read). Same refusals as
    _read_items_strict -- symlink, FIFO and oversize all come back as None,
    which callers read as "unknown" and fall back to a verbatim write.

    b"" means a genuinely empty queue, which is NOT the same as None: an empty
    queue is known state that a stale in-memory copy must not repopulate.
    """
    try:
        if not path.exists():
            return b""
        from .config import _open_regular_readonly
        fd = _open_regular_readonly(path)
    except OSError:
        return None
    try:
        # fstat on the HELD fd, for the same reason as everywhere else: the
        # path may be swapped for something huge between check and read.
        if os.fstat(fd).st_size > MAX_QUEUE_BYTES:
            return None
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _read_items_quiet(path: Path) -> Optional[list[DownloadItem]]:
    """Best-effort read for the save-time merge.

    Returns [] when there is nothing to merge with (no file yet -- a
    known-empty queue, which a stale in-memory copy must not repopulate) and
    None when the file exists but cannot be understood, which means "unknown"
    and makes the caller write its own rows verbatim exactly as it did before
    the lock existed.

    Never raises and never quarantines the file: load() owns that policy.
    """
    try:
        if not path.exists():
            return []
        return _read_items_strict(path)[0]
    except (OSError, ValueError, TypeError, RuntimeError):
        return None


def _merge_items(base_ids: set, mine: list, theirs: list) -> list[DownloadItem]:
    """Merge this store's rows with the file's, told apart by *base_ids*.

    `base_ids` is the set of ids this store last saw on disk. It is the entire
    disambiguation: without it, "row X is missing" and "row X was never seen"
    are the same fact, and the only merge that never loses an add -- union with
    the file -- also resurrects every row the user deleted. That is exactly how
    a previous merge-on-save attempt broke "Remove": the removal was undone by
    the next save from another process.

    Rules:
      * file row, id in base_ids, id not in mine  -> we deleted it, drop it.
      * file row, id in mine -> our copy is the newer local edit, so ours wins,
        but it keeps THEIR slot in the list (oldest-first order is meaningful).
      * file row, id not in base_ids -> somebody else added it after we last
        synced; leave it exactly as they wrote it. This is the rule that stops
        a stale GUI snapshot from erasing a `tg-dl download` batch.
      * my row, id not in base_ids -> new to the file, append it.
      * my row, id in base_ids but absent from the file -> somebody else
        deleted it (a "Clear all", a `tg-dl config reset`). Republishing it
        would resurrect the very rows a deletion exists to remove, so drop it.
    """
    mine_ids = {it.id for it in mine}
    first: dict = {}
    for it in mine:
        first.setdefault(it.id, it)
    merged: list[DownloadItem] = []
    overlaid: set = set()
    placed: set = set()
    for d in theirs:
        if d.id in base_ids and d.id not in mine_ids:
            continue                      # our own deletion
        src = d
        if d.id in first and d.id not in overlaid:
            overlaid.add(d.id)
            src = first[d.id]             # ours wins, their position kept
        merged.append(src)
        placed.add(d.id)
    for m in mine:
        if m.id not in placed and m.id not in base_ids:
            merged.append(m)              # new to the file
            placed.add(m.id)
    return merged


class QueueStore:
    """In-memory list with JSON persistence + change listeners (for GUI)."""

    def __init__(self, path: Path = QUEUE_FILE):
        self.path = path
        self.items: list[DownloadItem] = []
        self._listeners: list[Callable[[], None]] = []
        # Ids as they were on disk the last time this store synced with it --
        # the base snapshot _merge_items needs to tell a deliberate deletion
        # from a row it has simply never seen. None means "no comparable
        # snapshot": the file was loaded from rows whose ids were not persisted
        # (so every re-read mints new ones), and the store falls back to the
        # pre-lock overwrite rather than reconcile against garbage. A fresh
        # save persists the ids and makes the base usable from then on.
        # _mutex is what keeps items/_base_ids consistent when the GUI
        # downloads on a worker thread while the UI thread mutates the list.
        self._base_ids: Optional[set] = set()
        # Exact bytes of the last successful publication. When the file still
        # matches them, no other process has touched it and the merge provably
        # collapses to self.items -- which is what lets save() skip parsing.
        self._written: Optional[bytes] = None
        self._mutex = threading.RLock()
        self.load()

    # -- persistence -----------------------------------------------------
    def load(self) -> None:
        # A missing file is a known-empty queue, so the empty base is honest
        # and merging works from the very first add(). Only a file that WAS
        # there and turned out to carry unstable ids clears this.
        stable = True
        try:
            if self.path.is_symlink():
                raise ValueError("refusing to load queue through symlink")
            if self.path.exists():
                self.items, stable = _read_items_strict(self.path)
                # Anything left mid-flight when the app closed goes back to queued.
                for it in self.items:
                    if it.status in ("downloading", "converting", "fetching"):
                        it.status = "queued"
                harden_private_file(self.path)
        except (OSError, ValueError, TypeError, RuntimeError):
            # Corruption must never crash startup: back up then start clean.
            # (RecursionError, a RuntimeError subclass, can be raised by
            # json.load on adversarial nesting; TypeError by json parse.)
            # Cap pile-up: keep only last 5 backups, delete oldest.
            try:
                if self.path.exists() and not self.path.is_symlink():
                    # Prune old corrupt backups
                    try:
                        corrupt = sorted(self.path.parent.glob(f"{self.path.name}.corrupt-*.bak"))
                        while len(corrupt) >= 5:
                            try:
                                corrupt[0].unlink()
                            except OSError:
                                pass
                            corrupt = corrupt[1:]
                    except OSError:
                        pass
                    bak = self.path.with_name(
                        f"{self.path.name}.corrupt-{int(time.time())}-{uuid.uuid4().hex[:6]}.bak")
                    self.path.replace(bak)
                    harden_private_file(bak)
            except OSError:
                pass
            self.items = []
        self._base_ids = {it.id for it in self.items} if stable else None

    def save(self) -> None:
        """Persist self.items without losing another process's concurrent work.

        The whole read-merge-write runs under one flock, because the file is a
        read-modify-write unit: `_atomic_write` makes a single write
        indivisible, but two writers that both read first still overwrite each
        other. Measured before the lock: 4 processes x 250 add() left 272 of
        1000 rows with no warning.

        The lock is bounded (see config.LOCK_TIMEOUT_S) and is skipped
        entirely where fcntl does not exist, so the worst case degrades to the
        old last-writer-wins behaviour -- a lost row, never a corrupt file.
        """
        with self._mutex:
            try:
                with interprocess_lock(self.path):
                    self._save_locked()
            except OSError as exc:
                # Covers the lock's own teardown too, which can fail on a
                # filesystem without working flock.
                print(f"Warning: could not persist queue to {self.path}: {exc}", file=sys.stderr)

    def _save_locked(self) -> None:
        # Merge against the file first. Writing self.items verbatim is
        # last-writer-wins over a snapshot that may be minutes old: the GUI
        # holds a QueueStore for its whole lifetime, so every save it made was
        # discarding whatever `tg-dl download` had added in the meantime.
        raw = _read_queue_bytes(self.path)
        if raw is None or self._base_ids is None:
            # Unreadable/corrupt, or rows whose ids are not persisted yet: no
            # merge. Write our own rows verbatim, which is both the pre-lock
            # behaviour and the self-healing path.
            items = list(self.items)
        elif raw == self._written:
            # Fast path: the file is byte-for-byte what we published, so every
            # row in it is one we still hold and no other writer has been near
            # it. _merge_items would then emit exactly self.items: the file's
            # order IS the order we published, and every local mutation
            # (add/extend append at the end, remove/clear filter in place,
            # update never reorders) preserves it. Skipping the parse here is
            # worth it -- see _read_queue_bytes.
            items = list(self.items)
        else:
            theirs = _read_items_quiet(self.path)
            if theirs is None:
                # Unparseable between the two reads (or a writer that we do
                # not share a lock with, on a platform without fcntl).
                items = list(self.items)
            else:
                items = _merge_items(self._base_ids, self.items, theirs)
        # Build the payload on a local list. A *save* must not half-mutate
        # live state: the old code evicted from self.items in place and then
        # bailed out with "save skipped", leaving 999 of 1000 items destroyed
        # in memory while the file on disk kept the original contents.
        if len(items) > MAX_QUEUE_ITEMS:
            print(f"Warning: queue overflow, dropped {len(items) - MAX_QUEUE_ITEMS} oldest item(s)", file=sys.stderr)
            items = items[-MAX_QUEUE_ITEMS:]

        def _dump(seq):
            # allow_nan=False: writing NaN/Infinity produced a file the
            # loader was contractually forbidden to read.
            return json.dumps([i.to_dict() for i in seq], indent=2,
                              allow_nan=False).encode("utf-8")

        try:
            data = _dump(items)
        except ValueError as exc:
            print(f"Warning: queue not saved, non-finite value: {exc}", file=sys.stderr)
            return
        try:
            # Evict oldest-first until it fits. items[0] is the oldest, since
            # extend() appends.
            dropped = 0
            while len(data) > MAX_QUEUE_BYTES and len(items) > 1:
                items = items[1:]
                dropped += 1
                data = _dump(items)
            if len(data) > MAX_QUEUE_BYTES:
                # A single item alone is too big. Leave memory untouched and
                # skip the save rather than corrupting the live queue.
                print("Warning: queue data exceeds size limit, save skipped", file=sys.stderr)
                return
            if dropped:
                print(f"Warning: dropped {dropped} oldest item(s) to fit size limit", file=sys.stderr)
            # Only now, with a payload that actually fits, commit both the
            # file and the in-memory view so they cannot disagree. Refreshing
            # items with the merged list is what keeps _base_ids and items in
            # step -- and it means the GUI now *sees* rows another process
            # added instead of silently erasing them on its next save.
            _atomic_write(self.path, data)
            self.items = items
            self._base_ids = {it.id for it in items}
            self._written = data
        except OSError as exc:
            # Was silently swallowed: ENOSPC/EROFS meant the queue was never
            # persisted with no diagnostic at all.
            print(f"Warning: could not persist queue to {self.path}: {exc}", file=sys.stderr)

    # -- mutation --------------------------------------------------------
    def on_change(self, fn: Callable[[], None]) -> None:
        self._listeners.append(fn)

    def _emit(self) -> None:
        self.save()
        for fn in self._listeners:
            try:
                fn()
            except Exception:
                pass

    def add(self, item: DownloadItem) -> DownloadItem:
        if len(self.items) >= MAX_QUEUE_ITEMS:
            return item
        self.items.append(_sanitize_item(item))
        self._emit()
        return item

    def extend(self, items: list[DownloadItem]) -> list[DownloadItem]:
        """Add many items with a single save (avoids O(n²) I/O on batches).

        Returns exactly the items actually added, so callers can address
        them by id without guessing from queue length (queue may silently
        truncate when near MAX_QUEUE_ITEMS).
        """
        room = MAX_QUEUE_ITEMS - len(self.items)
        if room <= 0:
            return []
        added = [_sanitize_item(it) for it in items[:room]]
        self.items.extend(added)
        self._emit()
        return added

    def get(self, item_id: str) -> Optional[DownloadItem]:
        for it in self.items:
            if it.id == item_id:
                return it
        return None

    def update(self, item_id: str, **kw) -> None:
        it = self.get(item_id)
        if not it:
            return
        for k, v in kw.items():
            if k in _FORBIDDEN_KEYS or k.startswith("__"):
                continue  # dunder/object attrs can never be set (DoS guard)
            if k not in _ALLOWED_FIELDS:
                continue  # method names (to_dict/from_dict) shadow nothing here
            if k == "id" and (not isinstance(v, str) or not v or len(v) > 64):
                continue
            if k == "ext" and isinstance(v, str):
                if v and not _EXT_RE.fullmatch(v):
                    continue
            if k == "status" and v not in STATUSES:
                continue
            if k == "mode" and v not in _MODES:
                continue
            if k == "progress":
                try:
                    v = _sanitize_num(v, is_float=True)
                except (TypeError, ValueError, OverflowError):
                    continue
            if k in ("size", "current_bytes", "total_bytes"):
                try:
                    v = _sanitize_num(v)
                except (TypeError, ValueError, OverflowError):
                    continue
            if isinstance(v, bool):
                continue
            if not isinstance(v, (str, int, float)):
                continue
            setattr(it, k, v)
        # update() does not run the from_dict gate; run the same single
        # validator so e.g. empty ids / non-string ext can never persist.
        _sanitize_item(it)
        self._emit()

    def remove(self, item_id: str) -> None:
        self.items = [i for i in self.items if i.id != item_id]
        self._emit()

    def clear_finished(self) -> None:
        self.items = [i for i in self.items if i.status not in ("done", "cancelled")]
        self._emit()

    def clear_all(self) -> None:
        self.items = []
        self._emit()

    def pending(self) -> list[DownloadItem]:
        return [i for i in self.items if i.status in ("queued", "error")]

    def reset_errors_to_queued(self) -> None:
        for it in self.items:
            if it.status in ("error", "cancelled"):
                it.status = "queued"
                it.error = ""
        self._emit()

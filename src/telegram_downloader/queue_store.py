"""Download queue model + JSON persistence (resume-all across restarts)."""
from __future__ import annotations

import json
import math
import os
import re
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Optional

from .config import QUEUE_FILE, _atomic_write, harden_private_file
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
    except (TypeError, ValueError):
        return 0.0 if is_float else 0
    if is_float:
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


class QueueStore:
    """In-memory list with JSON persistence + change listeners (for GUI)."""

    def __init__(self, path: Path = QUEUE_FILE):
        self.path = path
        self.items: list[DownloadItem] = []
        self._listeners: list[Callable[[], None]] = []
        self.load()

    # -- persistence -----------------------------------------------------
    def load(self) -> None:
        try:
            if self.path.is_symlink():
                raise ValueError("refusing to load queue through symlink")
            if self.path.exists():
                # O_NOFOLLOW open + fstat: closes the check-then-read swap
                # window (a link planted after is_symlink() raises ELOOP).
                nofollow = getattr(os, "O_NOFOLLOW", 0)
                try:
                    fd = os.open(self.path, os.O_RDONLY | nofollow)
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
                        raise ValueError("queue file too large")
                except OSError:
                    pass
                try:
                    with os.fdopen(fd, "r", encoding="utf-8") as fh:
                        fd = -1
                        raw = json.load(
                            fh,
                            parse_constant=lambda x: (_ for _ in ()).throw(
                                ValueError("non-finite number in queue")),
                        )
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
                self.items = [DownloadItem.from_dict(d) for d in raw]
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

    def save(self) -> None:
        try:
            if len(self.items) > MAX_QUEUE_ITEMS:
                print(f"Warning: queue overflow, dropped {len(self.items) - MAX_QUEUE_ITEMS} oldest item(s)", file=sys.stderr)
                self.items = self.items[-MAX_QUEUE_ITEMS:]
            data = json.dumps([i.to_dict() for i in self.items], indent=2).encode("utf-8")
            # Never silently drop the whole save when oversized: evict
            # oldest items until it fits (active downloads are newest-first
            # in practice since extend appends).
            while len(data) > MAX_QUEUE_BYTES and len(self.items) > 1:
                self.items = self.items[1:]
                data = json.dumps([i.to_dict() for i in self.items], indent=2).encode("utf-8")
            if len(data) > MAX_QUEUE_BYTES:
                print("Warning: queue data exceeds size limit, save skipped", file=sys.stderr)
                return
            _atomic_write(self.path, data)
        except OSError:
            pass

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
                except (TypeError, ValueError):
                    continue
            if k in ("size", "current_bytes", "total_bytes"):
                try:
                    v = _sanitize_num(v)
                except (TypeError, ValueError):
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

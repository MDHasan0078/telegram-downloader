"""Download queue model + JSON persistence (resume-all across restarts)."""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .config import QUEUE_FILE, _atomic_write, harden_private_file

STATUSES = ("queued", "preview", "fetching", "downloading", "converting",
            "done", "error", "cancelled")

MAX_QUEUE_ITEMS = 1000
MAX_QUEUE_BYTES = 5 * 1024 * 1024
MAX_STR_FIELD = 500


@dataclass
class DownloadItem:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    url: str = ""
    title: str = ""          # final stem without extension (editable)
    ext: str = ".mp4"
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
        try:
            if "progress" in d:
                d = dict(d)
                d["progress"] = max(0.0, min(100.0, float(d.get("progress") or 0)))
            for numkey in ("size", "current_bytes", "total_bytes"):
                if numkey in d:
                    d = dict(d)
                    d[numkey] = max(0, min(8 * 1024 ** 3, int(d.get(numkey) or 0)))
        except (TypeError, ValueError):
            pass
        for k, v in d.items():
            if k == "status" and v not in STATUSES:
                continue  # corrupt status: leave as default "queued"
            if k == "id" and (not isinstance(v, str) or len(v) > 64):
                continue  # corrupt id: keep the generated one
            if k in ("url", "title", "ext", "error", "dest") and isinstance(v, str):
                if len(v) > MAX_STR_FIELD:
                    v = v[:MAX_STR_FIELD]
            if k == "ext" and isinstance(v, str):
                import re as _re
                if v and not _re.fullmatch(r"\.[A-Za-z0-9]{1,10}", v):
                    continue
            if hasattr(it, k) and isinstance(v, (str, int, float)) and not isinstance(v, bool):
                setattr(it, k, v)
        return it


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
                try:
                    if self.path.stat().st_size > MAX_QUEUE_BYTES:
                        raise ValueError("queue file too large")
                except OSError:
                    pass
                raw = json.loads(self.path.read_text(encoding="utf-8"))
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
        except (OSError, ValueError):
            # Never lose user data silently: back up the corrupt file first.
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
                self.items = self.items[-MAX_QUEUE_ITEMS:]
            data = json.dumps([i.to_dict() for i in self.items], indent=2).encode("utf-8")
            if len(data) > MAX_QUEUE_BYTES:
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
        # Clamp oversized fields on the way in (mirrors from_dict).
        if len(item.title) > MAX_STR_FIELD:
            item.title = item.title[:MAX_STR_FIELD]
        if len(item.error) > MAX_STR_FIELD:
            item.error = item.error[:MAX_STR_FIELD]
        if len(item.dest) > MAX_STR_FIELD:
            item.dest = item.dest[:MAX_STR_FIELD]
        if len(item.url) > MAX_STR_FIELD:
            item.url = item.url[:MAX_STR_FIELD]
        self.items.append(item)
        self._emit()
        return item

    def extend(self, items: list[DownloadItem]) -> None:
        """Add many items with a single save (avoids O(n²) I/O on batches)."""
        room = MAX_QUEUE_ITEMS - len(self.items)
        if room <= 0:
            return
        # Clamp fields like add() does — prevents store.extend() bypass.
        clamped: list[DownloadItem] = []
        for it in items[:room]:
            if len(it.title) > MAX_STR_FIELD:
                it.title = it.title[:MAX_STR_FIELD]
            if len(it.error) > MAX_STR_FIELD:
                it.error = it.error[:MAX_STR_FIELD]
            if len(it.dest) > MAX_STR_FIELD:
                it.dest = it.dest[:MAX_STR_FIELD]
            if len(it.url) > MAX_STR_FIELD:
                it.url = it.url[:MAX_STR_FIELD]
            if it.ext and not __import__("re").fullmatch(r"\.[A-Za-z0-9]{1,10}", it.ext):
                it.ext = ".mp4"
            # Numeric caps also clamped (mirrors from_dict)
            try:
                it.size = max(0, min(8 * 1024 ** 3, int(it.size or 0)))
                it.current_bytes = max(0, min(8 * 1024 ** 3, int(it.current_bytes or 0)))
                it.total_bytes = max(0, min(8 * 1024 ** 3, int(it.total_bytes or 0)))
                it.progress = max(0.0, min(100.0, float(it.progress or 0)))
            except (TypeError, ValueError):
                pass
            if it.status not in STATUSES:
                it.status = "queued"
            clamped.append(it)
        self.items.extend(clamped)
        self._emit()

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
            if not hasattr(it, k):
                continue
            # Reuse from_dict validation so callers can't bypass caps.
            if k in ("url", "title", "error", "dest") and isinstance(v, str):
                v = v[:MAX_STR_FIELD]
            if k == "ext" and isinstance(v, str):
                import re as _re
                if v and not _re.fullmatch(r"\.[A-Za-z0-9]{1,10}", v):
                    continue
            if k == "status" and v not in STATUSES:
                continue
            if k == "id" and (not isinstance(v, str) or len(v) > 64):
                continue
            if k == "progress":
                try:
                    v = max(0.0, min(100.0, float(v or 0)))
                except (TypeError, ValueError):
                    continue
            if k in ("size", "current_bytes", "total_bytes"):
                try:
                    v = max(0, min(8 * 1024 ** 3, int(v or 0)))
                except (TypeError, ValueError):
                    continue
            if isinstance(v, bool):
                continue
            if not isinstance(v, (str, int, float)):
                continue
            setattr(it, k, v)
        self._emit()

    def remove(self, item_id: str) -> None:
        self.items = [i for i in self.items if i.id != item_id]
        self._emit()

    def clear_finished(self) -> None:
        self.items = [i for i in self.items if i.status not in ("done", "cancelled")]
        self._emit()

    def pending(self) -> list[DownloadItem]:
        return [i for i in self.items if i.status in ("queued", "error")]

    def reset_errors_to_queued(self) -> None:
        for it in self.items:
            if it.status in ("error", "cancelled"):
                it.status = "queued"
                it.error = ""
        self._emit()

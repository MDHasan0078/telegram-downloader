"""The engine glue: the shared Python the GTK app drives.

Everything that is not a widget lives here -- the Telegram client, the queue
worker, the download-folder handling -- and it is all IMPORTED from the
engine modules, never reimplemented:

    config, downloader, converter, deps, updates, queue_store, tg_client

The one thing that has to be re-plumbed is the concurrency. The Flet app is
asyncio on the UI thread; GTK4 is a main loop that must never block. So the
engine runs ONE asyncio event loop in a background thread (telethon needs
it), the GTK thread only ever touches widgets, and every hand-off between
them goes through `GLib.idle_add` -- which is the same contract the Flet
app had between its worker and its repaint.

The queue worker's logic, the identity pin, the cancel semantics and the
message strings are ported from gui.py, not rewritten.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib  # noqa: E402 -- the hand-off to the GTK thread

from ..config import (clear_session, ensure_download_dir,
                      harden_session_files, load_settings, save_settings)
from ..converter import ffmpeg_to_mp4
from ..queue_store import DownloadItem, QueueStore
from ..telegram_utils import (auto_title_from_message, format_bytes,
                              infer_extension, parse_message_url, safe_filename)
from .. import converter

# `downloader` is imported INSIDE the worker, not here: it pulls telethon at
# module level, and the GTK app has to start (and show its pages) on a
# machine where telethon is not installed yet. The download itself still
# needs it -- that is a packaging question, not a code one.

# The store's seven states -> the six-state vocabulary the marks are keyed
# on. This is the only translation point, exactly as in gui.py.
_STORE_TO_UI = {
    "queued": "waiting",
    "fetching": "working",
    "downloading": "working",
    "converting": "working",
    "done": "done",
    "error": "failed",
    "cancelled": "cancelled",
}

_IDLE_LABEL = {
    "queued": "Queued — waiting to start",
    "cancelled": "Cancelled — partial kept for resume",
}


def _sanitize_display(s: str, limit: int = 500) -> str:
    """The same sanitiser the config layer uses, re-exported so the views do
    not import a private name."""
    from ..config import _sanitize_display as _impl
    return _impl(s, limit)


def progress_detail(it) -> str:
    """Everything under a live row's bar: bytes, speed and phase."""
    if it.status == "fetching":
        return "Looking up file details…"
    speed = getattr(it, "download_speed", 0) or 0
    speed_txt = f"  •  {format_bytes(speed)}/s" if speed > 0 else ""
    return (f"{format_bytes(it.current_bytes)}/{format_bytes(it.total_bytes)}"
            + speed_txt + "  •  " + ("converting…" if it.status == "converting"
                                     else "downloading…"))


def row_sig(it) -> tuple:
    """Everything a queue row DISPLAYS, as a comparable key."""
    return (it.status, it.title, it.ext, it.error, it.dest)


class Engine:
    """The shared behaviour, on its own event loop, off the GTK thread."""

    def __init__(self, store: QueueStore | None = None):
        self.store = store if store is not None else QueueStore()
        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._client = None
        self._client_fp = None
        self._me_id = None
        self._cancel: asyncio.Event | None = None
        self.downloading = False
        self.queuing = False
        # Set by the shell. Called (already marshalled onto the GTK thread)
        # whenever the queue changes.
        self.on_queue_changed = None
        # Set by the shell: a transient message at the bottom of the window.
        self.on_message = None

    # -- the loop ---------------------------------------------------------

    def start(self) -> None:
        """Start the background event loop."""
        if self._thread is not None:
            return
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True,
                                        name="tgdl-engine")
        self._thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro):
        """Schedule a coroutine on the engine loop; returns a Future."""
        if self.loop is None:
            self.start()
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def run_async(self, handler, done) -> None:
        """Run a coroutine function and report back on the GTK thread.

        `done(ok, value)` -- value is the exception on failure. The GTK
        thread never blocks: the coroutine runs on the engine loop.
        """
        def _wrapped():
            try:
                value = asyncio.run_coroutine_threadsafe(
                    handler(), self.loop).result()
            except Exception as exc:      # noqa: BLE001 - reported, not raised
                GLib.idle_add(done, False, exc)
                return
            GLib.idle_add(done, True, value)
        threading.Thread(target=_wrapped, daemon=True).start()

    def call_soon(self, fn, *args) -> None:
        """Run a callable on the GTK thread."""
        GLib.idle_add(fn, *args)

    # -- the client -------------------------------------------------------

    async def get_client(self):
        """The Telegram client, pinned to one account.

        Mirrors gui.py's get_client(): a planted session for a different
        account is refused instead of auto-trusted, and the pin is NOT
        cleared on refusal -- the only way out is the explicit "Log out"
        action, or the guard becomes one-shot and a second attempt adopts
        whatever session is present.
        """
        from .. import tg_client
        st = load_settings()
        if not st.api_id or not st.api_hash:
            raise RuntimeError("Enter API ID + hash in Account auth first.")
        fingerprint = (str(st.api_id), str(st.api_hash))
        if self._client is None or self._client_fp != fingerprint:
            # Credentials changed (or first use): drop the stale client so
            # one account's session is never driven with another's keys.
            old = self._client
            if old is not None:
                try:
                    await old.disconnect()
                except Exception:
                    pass
            self._client = tg_client.build_client(st.api_id_int(),
                                                  str(st.api_hash))
            self._client_fp = fingerprint
            self._me_id = None
        c = self._client
        if not c.is_connected():
            await c.connect()
            harden_session_files()
        if self._me_id is None and await c.is_user_authorized():
            try:
                me = await c.get_me()
                me_id = str(getattr(me, "id", "") or "")
                pinned = ((st.user_id or "").strip()
                          if getattr(st, "user_id", None) else "")
                if pinned and me_id and pinned != me_id:
                    try:
                        await c.log_out()
                    except Exception:
                        pass
                    try:
                        await c.disconnect()
                    except Exception:
                        pass
                    self._client = None
                    self._client_fp = None
                    clear_session()
                    raise RuntimeError(
                        "A different Telegram account is signed in than the one "
                        "this install is pinned to. For safety you have been "
                        "signed out. If that is expected, use 'Log out' in "
                        "Account auth and sign in again; otherwise this "
                        "session file was replaced by someone else.")
                if me_id:
                    self._me_id = me_id
            except RuntimeError:
                raise
            except Exception:
                pass
        return c

    # -- the queue --------------------------------------------------------

    def notify(self, message: str, error: bool = False) -> None:
        if self.on_message is not None:
            self.on_message(message, error)

    def refresh(self) -> None:
        if self.on_queue_changed is not None:
            self.on_queue_changed()

    async def start_queue(self) -> None:
        """The download worker.

        Ported from gui.py's start_queue(): the re-drain (pick queued items
        one at a time, so "Retry" or "Resume all" during a run is honoured
        instead of lost), the cancel race on the metadata fetch, the
        symlink-refusing publish, and the mode-2/3 conversion with its
        one-hour deadline.
        """
        if self.downloading:
            return
        if not any(i.status == "queued" for i in self.store.items):
            self.notify("Queue is empty (or all done). "
                        "Retry an error or add new links.")
            return
        from ..downloader import download_resumable
        self.downloading = True
        self._cancel = asyncio.Event()
        self.refresh()
        try:
            c = await self.get_client()
            st = load_settings()
            out_dir = ensure_download_dir(st)
            default_mode = st.output_mode or "1"
            while not self._cancel.is_set():
                item = next((i for i in self.store.items if i.status == "queued"),
                            None)
                if item is None:
                    break
                self.store.update(item.id, status="downloading", error="")
                mode = item.mode or default_mode

                def _cb(cur, tot, _id=item.id, _last=[0.0], _start=[0.0]):
                    if _start[0] == 0.0:
                        _start[0] = time.monotonic()
                    it = self.store.get(_id)
                    if it:
                        it.current_bytes = cur
                        it.total_bytes = tot
                        it.progress = (cur * 100 / tot) if tot else 0
                        elapsed = time.monotonic() - _start[0]
                        it.download_speed = (cur / elapsed) if elapsed > 0 else 0
                    # Throttle: redraw at most ~4x/s.
                    now = time.monotonic()
                    if now - _last[0] >= 0.25:
                        _last[0] = now
                        self.refresh()
                try:
                    try:
                        chat, mid, _ = parse_message_url(item.url)
                    except ValueError as exc:
                        self.store.update(item.id, status="error",
                                          error=f"Invalid message URL: {exc}")
                        self.refresh()
                        continue
                    try:
                        # Race the metadata fetch against Cancel: without
                        # this a stuck get_messages holds the item
                        # "downloading" for up to 120 s with no way to stop.
                        _mget = asyncio.ensure_future(c.get_messages(chat,
                                                                     ids=mid))
                        _cwait = asyncio.ensure_future(self._cancel.wait())
                        done, _pending = await asyncio.wait(
                            {_mget, _cwait},
                            return_when=asyncio.FIRST_COMPLETED, timeout=120)

                        async def _cancel_pending():
                            for t in (_mget, _cwait):
                                if not t.done():
                                    t.cancel()
                            await asyncio.gather(*_pending,
                                                 return_exceptions=True)

                        if self._cancel.is_set():
                            await _cancel_pending()
                            raise asyncio.CancelledError(
                                "Cancelled while waiting for message metadata.")
                        if _mget in done:
                            msg = _mget.result()
                            if not _cwait.done():
                                _cwait.cancel()
                            await asyncio.gather(_cwait, return_exceptions=True)
                        else:
                            await _cancel_pending()
                            raise RuntimeError(
                                "Telegram didn't respond within 2 minutes. "
                                "The service may be busy — please try again "
                                "shortly.")
                    except asyncio.TimeoutError as exc:
                        raise RuntimeError(
                            "Telegram didn't respond within 2 minutes. "
                            "The service may be busy — please try again "
                            "shortly.") from exc
                    if not msg or not getattr(msg, "media", None):
                        raise RuntimeError("Message has no downloadable media.")
                    ext = infer_extension(msg)
                    stem = safe_filename(item.title
                                         or auto_title_from_message(msg, chat))
                    try:
                        self.store.update(
                            item.id, title=stem, ext=ext,
                            size=int(getattr(getattr(msg, "file", None),
                                             "size", 0) or 0))
                    except Exception:
                        pass
                    source = out_dir / f"{stem}{ext}"
                    target = out_dir / f"{stem}.mp4"
                    try:
                        out_res = out_dir.resolve()
                        if source.is_symlink() or target.is_symlink() or \
                                source.absolute().resolve().parent != out_res or \
                                target.absolute().resolve().parent != out_res:
                            raise RuntimeError("Refusing to write outside "
                                               "the download folder.")
                    except RuntimeError:
                        raise
                    except OSError:
                        pass
                    if mode in ("2", "3") and source.resolve() == target.resolve():
                        source = out_dir / f"{stem}.source{ext}"
                    await download_resumable(c, msg, source, progress_cb=_cb,
                                             cancel=self._cancel)
                    if mode in ("2", "3"):
                        if not converter.has_ffmpeg():
                            self.store.update(item.id, status="done",
                                              progress=100.0, dest=str(source),
                                              error="ffmpeg missing — kept original.")
                            continue
                        self.store.update(item.id, status="converting")
                        self.refresh()
                        import secrets as _secrets
                        import threading as _threading
                        tmp = target.with_name(
                            f"{target.stem}.part.{os.getpid()}."
                            f"{_secrets.token_hex(8)}.mp4")
                        nofollow = getattr(os, "O_NOFOLLOW", 0)
                        try:
                            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT
                                         | os.O_EXCL | nofollow, 0o600)
                            os.close(fd)
                            if tmp.is_symlink():
                                raise RuntimeError(
                                    "Refusing to publish through symlink.")
                        except FileExistsError:
                            raise RuntimeError("Refusing unsafe converter "
                                               "temp file (exists).")
                        except OSError as exc:
                            import errno as _errno
                            if exc.errno == _errno.ELOOP:
                                raise RuntimeError("Refusing to publish "
                                                   "through symlink.") from exc
                            raise
                        t_cancel = _threading.Event()

                        async def _watch_cancel():
                            await self._cancel.wait()
                            t_cancel.set()

                        async def _deadline(seconds: int = 3600):
                            # A hung ffmpeg must not hold the item
                            # "converting" forever: hard-stop after an hour.
                            await asyncio.sleep(seconds)
                            t_cancel.set()
                        watch = asyncio.create_task(_watch_cancel())
                        clock = asyncio.create_task(_deadline())
                        try:
                            await asyncio.to_thread(
                                ffmpeg_to_mp4, source, tmp, mode, cancel=t_cancel)
                        finally:
                            watch.cancel()
                            clock.cancel()
                        if self._cancel.is_set():
                            try:
                                tmp.unlink(missing_ok=True)
                            except OSError:
                                pass
                            self.store.update(item.id, status="cancelled",
                                              error="Cancelled by user.")
                            continue
                        if tmp.is_symlink():
                            try:
                                tmp.unlink()
                            except OSError:
                                pass
                            raise RuntimeError(
                                "Refusing to publish through symlink.")
                        if target.is_symlink():
                            tmp.unlink(missing_ok=True)
                            raise RuntimeError("Refusing to publish through "
                                               "symlink at target path.")
                        tmp.replace(target)
                        try:
                            if not source.is_symlink():
                                source.unlink(missing_ok=True)
                        except OSError:
                            pass
                        self.store.update(item.id, status="done", progress=100.0,
                                          dest=str(target))
                    else:
                        self.store.update(item.id, status="done", progress=100.0,
                                          dest=str(source))
                except asyncio.CancelledError as exc:
                    self.store.update(item.id, status="cancelled",
                                      error=_sanitize_display(str(exc)))
                except Exception as exc:      # noqa: BLE001 - reported per item
                    if self._cancel.is_set():
                        self.store.update(item.id, status="cancelled",
                                          error="Cancelled by user.")
                    else:
                        self.store.update(item.id, status="error",
                                          error=_sanitize_display(str(exc)))
                self.refresh()
        except Exception as exc:          # noqa: BLE001 - reported to the user
            self.notify(_sanitize_display(f"Download failed: {exc}"), error=True)
        finally:
            self.downloading = False
            self.refresh()

    def start_queue_async(self) -> None:
        self.submit(self.start_queue())

    def cancel_batch(self) -> None:
        """Stop the running batch. The current file is cancelled; the rest
        stay queued for "Resume all / Start"."""
        if self._cancel is not None and not self._cancel.is_set():
            loop = self.loop
            event = self._cancel
            if loop is not None:
                loop.call_soon_threadsafe(event.set)
            self.notify("Stopping queue — current file is cancelled, "
                        "the rest stay queued.")

    def cancel_item(self, target_id: str) -> None:
        it = self.store.get(target_id)
        if not it:
            return
        if it.status == "queued":
            self.store.update(target_id, status="cancelled",
                              error="Cancelled by user.")
            self.refresh()
            self.notify("Removed queued download.")
            return
        if it.status in ("downloading", "converting", "fetching"):
            # Only one batch can run at a time, so stopping the active item
            # naturally pauses the run. Be honest about it.
            if self._cancel is not None and not self._cancel.is_set():
                loop = self.loop
                event = self._cancel
                if loop is not None:
                    loop.call_soon_threadsafe(event.set)
                self.notify("Stopping this download — others stay queued "
                            "(Resume all to continue).")
            else:
                self.notify("Already stopping…", error=True)

    def toggle_pause(self, target_id: str) -> None:
        """Pause an active download, or resume a paused one.

        Users read the progress bar as a control, so clicking it has to do
        something. Pause goes through cancel_item, which sets the run's
        cancel event -- drastic, but it is the only stop primitive the
        engine has, and download_resumable() keeps the <name>.part plus its
        sidecar so the next run continues from the byte offset.
        """
        it = self.store.get(target_id)
        if not it:
            return
        if it.status in ("downloading", "converting", "fetching"):
            self.cancel_item(target_id)
            return
        if it.status == "cancelled":
            self.store.update(target_id, status="queued", error="")
            self.refresh()
            self.notify("Resuming — continues from where it stopped.")
            self.start_queue_async()
            return
        self.notify("Only a running or paused download can be paused.",
                    error=True)

    def retry_item(self, target_id: str) -> None:
        self.store.update(target_id, status="queued", error="")
        self.refresh()
        self.start_queue_async()

    def remove_item(self, target_id: str) -> None:
        self.store.remove(target_id)
        self.refresh()

    def clear_finished(self) -> None:
        self.store.clear_finished()
        self.refresh()

    def reset_errors_to_queued(self) -> None:
        self.store.reset_errors_to_queued()
        self.refresh()

    # -- the download folder ----------------------------------------------

    @staticmethod
    def folder_label() -> str:
        """The bottom bar's path, with the home directory shortened."""
        try:
            p = ensure_download_dir(load_settings())
            home = str(Path.home())
            s = str(p)
            return "~" + s[len(home):] if s.startswith(home + "/") else s
        except Exception:
            return "(not set)"

    def set_download_dir(self, chosen: str) -> bool:
        """Validate a folder and store it.

        ensure_download_dir(st) rejects paths inside the config dir,
        filesystem roots, and paths with shell metacharacters, so the bar can
        never display a folder that download time would then refuse.
        """
        try:
            st = load_settings()
            st.download_dir = str(chosen)
            ensure_download_dir(st)
            save_settings(st)
        except Exception as exc:          # noqa: BLE001 - shown to the user
            self.notify(f"Cannot use this folder: {exc}", error=True)
            return False
        self.notify("Download folder updated.")
        return True

    def open_folder(self) -> None:
        """Reveal the download folder in the desktop file manager."""
        def _redact_home(p: Path) -> str:
            try:
                home = str(Path.home())
                ps = str(p)
                return "~" + ps[len(home):] if ps.startswith(home + "/") else ps
            except Exception:
                return ps
        p = ensure_download_dir(load_settings())
        if not p.is_absolute():
            self.notify(f"Cannot use this folder: {_redact_home(p)}",
                        error=True)
            return
        try:
            # No `--`: old xdg-open does not support it, and is_absolute()
            # already kills option-injection. Resolved to absolute paths:
            # a hostile $PATH must not swap them.
            import shutil as _shutil
            if sys.platform.startswith("linux"):
                exe = _shutil.which("xdg-open")
                if not exe:
                    self.notify("Cannot open folder: xdg-open not found.",
                                error=True)
                    return
                subprocess.Popen([exe, str(p)])
            elif sys.platform == "darwin":
                subprocess.Popen([_shutil.which("open") or "/usr/bin/open",
                                  str(p)])
            elif sys.platform == "win32":
                subprocess.Popen(["explorer", str(p)])
            else:
                self.notify(f"Folder: {p}")
        except Exception as exc:          # noqa: BLE001 - shown to the user
            self.notify(f"{p} ({exc})")

    # -- the queue store's own helpers -------------------------------------

    def add_items(self, urls, mode: str) -> int:
        """Queue links. Returns how many were added."""
        items = [DownloadItem(url=u, mode=mode, status="queued") for u in urls]
        added = self.store.extend(items)
        self.refresh()
        return len(added)

    def clear_all(self) -> None:
        self.store.clear_all()
        self.refresh()

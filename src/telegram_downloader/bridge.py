"""The Kotlin <-> Python bridge for the native Android app.

Chaquopy runs this Python IN-PROCESS with the Android app: no socket, no
HTTP server, no Flutter. Kotlin calls the functions below directly, and each
one is a THIN wrapper over the engine that already exists:

    config, downloader, converter, deps, updates, queue_store, tg_client,
    telegram_utils

Nothing here re-implements telethon, the resumable downloader, the queue, the
auth flow, the identity pin or the updater's verification. This module only
exposes them, and it owns the ONE thing a UI cannot own: the concurrency.

THE THREADING SHAPE (the same one `gtk/engine.py` uses)

The engine is asyncio -- telethon requires it -- and the Android UI thread
must never block. So:

  * ONE asyncio event loop runs in a background thread ("tgdl-engine").
  * A call that needs the loop hops to that thread and WAITS for the result
    (`_Engine.run`). The wait happens on the CALLER's thread, which the
    Kotlin side always puts on a background dispatcher, never on the UI
    thread.
  * A long job (a download) is SUBMITTED, never awaited: `submit()` returns
    a future the UI does not block on. A download that takes minutes must
    not freeze the screen.

`gtk/engine.py` is the reference for this shape and for the worker itself;
the worker below is the same logic with the GTK bits removed.

THE CONFIG DIRECTORY

`config.py` resolves `~/.config/telegram-downloader/` and then REFUSES to
start if that directory is a symlink, is owned by somebody else, or is
group/world writable. On Android `~` is the app's internal storage
(Chaquopy sets `HOME` to it), which is app-private and survives reboots, but
the app still picks the location ITSELF rather than inheriting a default:
`init()` points `XDG_CONFIG_HOME` at `<files>/xdg`, created 0700, before
`config.py` is imported for the first time. Import order is the whole trick
-- `config.py` computes CONFIG_DIR at IMPORT time, so this module imports
NOTHING from the package at module level.

THE DOWNLOAD FOLDER

`~/Downloads/Telegram` does not exist on Android. The app passes its own
folder to `init()` (the app's external files dir, which is a real path on
the shared volume and writable without a runtime permission), and the user
can point it somewhere else with the system folder picker. The engine still
writes through the filesystem, because that is what the downloader does.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Optional

# NOTHING is imported from this package at module level: `config.py` computes
# CONFIG_DIR when it is imported, and that must happen AFTER init() has set
# the environment. See the module docstring.

# The store's seven states -> the six-state vocabulary the status marks are
# keyed on. The same translation as in gui.py and gtk/engine.py.
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

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_INIT_LOCK = threading.RLock()
_engine: Optional["_Engine"] = None

# Where the app keeps its things. Set by init().
_files_dir = ""
_cache_dir = ""
_default_download_dir = ""


def _require_engine() -> "_Engine":
    """The engine, or a clear error if init() has not run yet."""
    if _engine is None:
        raise RuntimeError(
            "telegram_downloader.bridge.init() must be called before any "
            "other bridge function.")
    return _engine


def _mkdir_private(path: str, mode: int = 0o700) -> str:
    """Create *path* (and its parents) owner-only. Never follows a symlink."""
    path = os.path.abspath(path)
    os.makedirs(path, mode=mode, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError:
        pass
    return path


# ---------------------------------------------------------------------------
# The engine: one event loop, in a background thread
# ---------------------------------------------------------------------------

class _Engine:
    """The shared behaviour, on its own event loop, off the UI thread.

    The same shape as `gtk/engine.py`: a worker thread owns the asyncio loop,
    the caller only ever submits work or waits for a result.
    """

    def __init__(self) -> None:
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._client = None
        self._client_fp: Optional[tuple] = None
        self._me_id: Optional[str] = None
        self._cancel: Optional[asyncio.Event] = None
        self.downloading = False
        self.store = None
        # The phone number of a login in progress, between the code request
        # and the code being typed in.
        self.pending_phone = ""

    # -- the loop ---------------------------------------------------------

    def start(self) -> None:
        """Start the background event loop."""
        if self.loop is not None:
            return
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True,
                                        name="tgdl-engine")
        self._thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self._thread_id = threading.get_ident()
        self.loop.run_forever()

    def submit(self, coro):
        """Schedule a coroutine on the engine loop. Returns a Future.

        Fire and forget: this is how a download that takes minutes is
        started without anyone waiting for it.
        """
        if self.loop is None:
            self.start()
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def run(self, coro, timeout: Optional[float] = None):
        """Run a coroutine on the engine loop and WAIT for the result.

        The wait is on the CALLER's thread. The Kotlin side always calls the
        bridge from a background dispatcher, so the Android UI thread is
        never the one blocked here.
        """
        if self._thread_id is not None and threading.get_ident() == self._thread_id:
            # Waiting for a result from the loop's own thread would deadlock.
            raise RuntimeError(
                "telegram_downloader.bridge: a blocking bridge call was made "
                "on the engine loop thread.")
        return self.submit(coro).result(timeout)

    # -- the client -------------------------------------------------------

    async def get_client(self):
        """The Telegram client, pinned to one account.

        Mirrors gui.py's get_client(): a planted session for a different
        account is refused instead of auto-trusted, and the pin is NOT
        cleared on refusal -- the only way out is the explicit "Log out"
        action.
        """
        from . import tg_client
        from .config import clear_session, harden_session_files, load_settings
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

    async def pin_identity(self) -> None:
        """Pin the account this session belongs to (the identity pin)."""
        from .config import load_settings, save_settings
        try:
            c = await self.get_client()
            me = await c.get_me()
            me_id = str(getattr(me, "id", "") or "")
            if me_id:
                st = load_settings()
                st.user_id = me_id
                save_settings(st)
                self._me_id = me_id
        except Exception:
            pass

    # -- the queue --------------------------------------------------------

    async def start_queue(self) -> None:
        """The download worker.

        Ported from gui.py's start_queue() and gtk/engine.py's: the
        re-drain (queued items one at a time, so "Retry" or "Resume all"
        during a run is honoured instead of lost), the cancel race on the
        metadata fetch, the symlink-refusing publish, and the mode-2/3
        conversion with its one-hour deadline.
        """
        from .converter import has_ffmpeg
        from .downloader import download_resumable
        from .config import ensure_download_dir, load_settings
        from .telegram_utils import (auto_title_from_message, infer_extension,
                                     parse_message_url, safe_filename)

        if self.downloading:
            return
        if not any(i.status == "queued" for i in self.store.items):
            return
        self.downloading = True
        self._cancel = asyncio.Event()
        try:
            c = await self.get_client()
            st = load_settings()
            out_dir = ensure_download_dir(_settings_with_download_dir(st))
            default_mode = st.output_mode or "1"
            while not self._cancel.is_set():
                item = next((i for i in self.store.items
                             if i.status == "queued"), None)
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

                try:
                    try:
                        chat, mid, _ = parse_message_url(item.url)
                    except ValueError as exc:
                        self.store.update(item.id, status="error",
                                          error=f"Invalid message URL: {exc}")
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
                        if not has_ffmpeg():
                            # No ffmpeg on Android: keep the original and say
                            # so, rather than failing at the end of a long
                            # download.
                            self.store.update(item.id, status="done",
                                              progress=100.0, dest=str(source),
                                              error="ffmpeg missing — kept the "
                                                    "original file.")
                            continue
                        self.store.update(item.id, status="converting")
                        import secrets as _secrets
                        tmp = target.with_name(
                            f"{target.stem}.part.{os.getpid()}."
                            f"{_secrets.token_hex(8)}.mp4")
                        from .converter import ffmpeg_to_mp4
                        import threading as _threading
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
                                ffmpeg_to_mp4, source, tmp, mode,
                                cancel=t_cancel)
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
                            raise RuntimeError(
                                "Refusing to publish through symlink at "
                                "target path.")
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
                                      error=_sanitize(str(exc)))
                except Exception as exc:      # noqa: BLE001 - reported per item
                    if self._cancel.is_set():
                        self.store.update(item.id, status="cancelled",
                                          error="Cancelled by user.")
                    else:
                        self.store.update(item.id, status="error",
                                          error=_sanitize(str(exc)))
        except Exception as exc:          # noqa: BLE001 - reported to the user
            _log(f"Download failed: {_sanitize(str(exc))}")
        finally:
            self.downloading = False

    def start_queue_async(self) -> None:
        """Start the worker without waiting for it."""
        self.submit(self.start_queue())

    def cancel_batch(self) -> None:
        """Stop the running batch. The current file is cancelled; the rest
        stay queued for "Resume all / Start"."""
        if self._cancel is not None and not self._cancel.is_set():
            loop = self.loop
            event = self._cancel
            if loop is not None:
                loop.call_soon_threadsafe(event.set)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _sanitize(value: str, limit: int = 500) -> str:
    """Strip control chars, for anything that reaches the screen."""
    from .config import _sanitize_display
    return _sanitize_display(value, limit)


def _log(message: str) -> None:
    """Python's stdout/stderr are redirected to logcat by Chaquopy."""
    print(f"[tgdl] {message}", flush=True)


def _app_version() -> str:
    """Never hardcode: read the installed metadata, fall back to the
    in-tree version."""
    try:
        from importlib.metadata import version as _v
        return _v("telegram-downloader")
    except Exception:
        try:
            from . import __version__ as _dev
            return _dev
        except Exception:
            return "0.0.0-unknown"


def _settings_with_download_dir(st):
    """The settings with the EFFECTIVE download folder filled in.

    An empty `download_dir` means "the app's own folder", which on Android
    is not `~/Downloads/Telegram` but whatever the app passed to init().
    """
    from .config import Settings
    if (st.download_dir or "").strip():
        return st
    if not _default_download_dir:
        return st
    return Settings(api_id=st.api_id, api_hash=st.api_hash,
                    download_dir=_default_download_dir,
                    output_mode=st.output_mode, theme=st.theme,
                    user_id=getattr(st, "user_id", None))


def _effective_download_dir() -> str:
    """The folder downloads actually go to, as a string."""
    from .config import load_settings
    st = load_settings()
    raw = (st.download_dir or "").strip()
    if raw:
        return raw
    if _default_download_dir:
        return _default_download_dir
    from .config import default_download_dir
    return str(default_download_dir())


# ---------------------------------------------------------------------------
# The public bridge: what Kotlin calls
# ---------------------------------------------------------------------------

def init(files_dir: str, download_dir: str = "", cache_dir: str = "") -> dict:
    """Prepare the app's paths and start the engine. Call this FIRST.

    `files_dir` is the app's internal storage (Chaquopy's HOME), `cache_dir`
    its cache, and `download_dir` the folder downloads go to unless the user
    picks another one.
    """
    global _engine, _files_dir, _cache_dir, _default_download_dir
    with _INIT_LOCK:
        if _engine is not None:
            return _status()
        _files_dir = os.path.realpath(os.path.abspath(files_dir or os.path.expanduser("~")))
        _cache_dir = (os.path.realpath(os.path.abspath(cache_dir))
                      if cache_dir else "")
        # HOME first: `Path.home()` is what `default_download_dir()` and the
        # fallback in config.py use, and it must be the app's own directory.
        os.environ["HOME"] = _files_dir
        if _cache_dir:
            try:
                _mkdir_private(_cache_dir)
                os.environ["TMPDIR"] = _cache_dir
            except OSError:
                pass
        # XDG_CONFIG_HOME: an explicit, owner-only directory. config.py
        # checks it (absolute, not a symlink, owned by us, not group/world
        # writable) and refuses to start otherwise -- so it is created 0700
        # BEFORE config.py is imported.
        xdg = _mkdir_private(os.path.join(_files_dir, "xdg"))
        os.environ["XDG_CONFIG_HOME"] = xdg
        if download_dir:
            _default_download_dir = os.path.realpath(os.path.abspath(download_dir))
            # The app's folder has to satisfy the engine's own checks (no
            # symlink anywhere in the chain, not the filesystem root, not
            # inside the config dir). If it does not -- a vendor volume with
            # an unexpected layout, say -- fall back to the app's internal
            # storage, which always works, and say so in the log. The app
            # shows the folder that is actually in use.
            try:
                from .config import Settings, ensure_download_dir
                ensure_download_dir(Settings(download_dir=_default_download_dir))
            except Exception as exc:      # noqa: BLE001 - reported, not raised
                _log(f"download folder {download_dir} refused ({exc}); "
                     f"using the app's internal folder instead")
                _default_download_dir = os.path.join(_files_dir, "Downloads",
                                                     "Telegram")
        # NOW the engine may be imported: config.py computes CONFIG_DIR at
        # import time, and the environment is finally right.
        from .queue_store import QueueStore
        engine = _Engine()
        engine.store = QueueStore()
        engine.start()
        _engine = engine
        return _status()


def _status() -> dict:
    """What the app needs to know about its own environment."""
    from .config import CONFIG_DIR
    return {
        "ok": True,
        "config_dir": str(CONFIG_DIR),
        "download_dir": _effective_download_dir(),
        "files_dir": _files_dir,
        "version": _app_version(),
        "python": ".".join(str(x) for x in __import__("sys").version_info[:3]),
    }


def get_settings() -> dict:
    """The persisted settings, plus what the app needs to render them."""
    from .config import load_settings
    st = load_settings()
    return {
        "api_id": st.api_id or "",
        "api_hash": st.api_hash or "",
        "has_api": st.has_api,
        "configured": st.is_configured,
        "download_dir": _effective_download_dir(),
        "output_mode": st.output_mode if st.output_mode in ("1", "2", "3") else "1",
        "theme": st.theme if st.theme in ("dark", "light") else "dark",
        "user_id": st.user_id or "",
    }


def set_api_credentials(api_id: str, api_hash: str) -> bool:
    """Store the Telegram API credentials. Thin over config.save_settings."""
    from .config import load_settings, save_settings
    api_id = (api_id or "").strip()
    api_hash = (api_hash or "").strip()
    if api_id and not api_id.isdigit():
        raise ValueError("API ID must be numeric.")
    st = load_settings()
    st.api_id = api_id or None
    st.api_hash = api_hash or None
    save_settings(st)
    return True


def set_output_mode(mode: str) -> bool:
    """Store the output mode (1=original, 2=fast, 3=max)."""
    from .config import load_settings, save_settings
    mode = str(mode or "1")
    if mode not in ("1", "2", "3"):
        raise ValueError("Unknown output mode.")
    st = load_settings()
    st.output_mode = mode
    save_settings(st)
    return True


def set_theme(theme: str) -> bool:
    """Store the theme ("dark" or "light")."""
    from .config import load_settings, save_settings
    theme = str(theme or "dark")
    if theme not in ("dark", "light"):
        raise ValueError("Unknown theme.")
    st = load_settings()
    st.theme = theme
    save_settings(st)
    return True


def get_download_dir() -> dict:
    """The folder downloads go to."""
    return {"ok": True, "download_dir": _effective_download_dir()}


def set_download_dir(path: str) -> dict:
    """Validate a folder and store it.

    `ensure_download_dir` refuses paths inside the config dir, filesystem
    roots, symlinks and paths with shell metacharacters, so the app can
    never display a folder that download time would then refuse.
    """
    from .config import ensure_download_dir, load_settings, save_settings
    st = load_settings()
    st.download_dir = str(path or "").strip()
    try:
        ensure_download_dir(st)
    except Exception as exc:          # noqa: BLE001 - shown to the user
        return {"ok": False, "error": f"Cannot use this folder: {exc}"}
    save_settings(st)
    return {"ok": True, "download_dir": st.download_dir}


def get_queue_items() -> list:
    """The download queue, as plain dicts."""
    from .telegram_utils import format_bytes
    engine = _require_engine()
    out = []
    for it in engine.store.items:
        speed = getattr(it, "download_speed", 0) or 0
        out.append({
            "id": it.id,
            "url": it.url,
            "title": it.title,
            "ext": it.ext,
            "mode": it.mode,
            "size": int(it.size or 0),
            "status": it.status,
            "ui_status": _STORE_TO_UI.get(it.status, "waiting"),
            "progress": float(it.progress or 0),
            "current_bytes": int(it.current_bytes or 0),
            "total_bytes": int(it.total_bytes or 0),
            "error": it.error,
            "dest": it.dest,
            "added_at": float(it.added_at or 0),
            "detail": _progress_detail(it, format_bytes),
            "idle_label": _IDLE_LABEL.get(it.status, ""),
            "download_speed": int(speed),
        })
    return out


def _progress_detail(it, format_bytes) -> str:
    """Everything under a live row's bar: bytes, speed and phase."""
    if it.status == "fetching":
        return "Looking up file details…"
    speed = getattr(it, "download_speed", 0) or 0
    speed_txt = f"  •  {format_bytes(speed)}/s" if speed > 0 else ""
    return (f"{format_bytes(it.current_bytes)}/{format_bytes(it.total_bytes)}"
            + speed_txt + "  •  " + ("converting…" if it.status == "converting"
                                     else "downloading…"))


def parse_links(raw: str) -> list:
    """Split pasted text into clean Telegram URLs. Thin over split_urls()."""
    from .telegram_utils import split_urls
    return split_urls(raw or "")


def add_links(urls: list) -> dict:
    """Queue links and start the run.

    The credentials are checked first (the app sends the user to Account
    auth if they are missing), then the URLs are split, queued, and the
    worker is started. The worker is SUBMITTED, not awaited: the UI thread
    must not wait for a download.
    """
    from .config import load_settings
    from .queue_store import DownloadItem
    from .telegram_utils import split_urls
    engine = _require_engine()
    st = load_settings()
    if not st.api_id or not st.api_hash:
        return {"ok": False, "added": 0, "requested": 0,
                "error": "First add your Telegram API ID + hash in "
                         "Account auth."}
    text = "\n".join(u for u in (urls or []) if isinstance(u, str))
    clean = split_urls(text)
    if not clean:
        return {"ok": False, "added": 0, "requested": 0,
                "error": "Paste at least one t.me link first."}
    mode = st.output_mode or "1"
    items = [DownloadItem(url=u, mode=mode, status="queued") for u in clean]
    added = engine.store.extend(items)
    engine.start_queue_async()
    return {"ok": True, "added": len(added), "requested": len(clean),
            "error": "" if len(added) == len(clean)
            else f"Queue near full: added {len(added)} of {len(clean)} "
                 f"link(s)."}


def get_dependencies() -> list:
    """The dependency list, with Android's truth in it.

    `deps.check_all()` is the shared list; the install commands it carries
    are for a desktop, so they are replaced here with what is actually true
    on Android -- above all that ffmpeg is NOT available, which is why the
    Fast and Max modes cannot work.

    A dependency that does not apply to this platform AT ALL is marked
    `applicable: False` rather than dropped. The flet row is the one: the
    desktop app really does ship flet (the .deb carries it for
    `tg-dl gui --flet` and for the web build), and its MISSING badge is
    correct there. This app is Kotlin/Compose and never imports flet, so
    the row is a false alarm here -- and the native screen does not show
    a row that says so. The desktop's own list is untouched.
    """
    from . import deps as deps_mod

    def _check() -> list:
        out = []
        for d in deps_mod.check_all():
            name = (d.name or "").lower()
            out.append({
                "name": d.name,
                "ok": bool(d.ok),
                # Two rules, and both have to hold for the row to be
                # shown: the shared list may say the dependency does not
                # apply to this platform at all (ffmpeg and cryptg on a
                # phone), and flet belongs to the DESKTOP app -- this one
                # is Kotlin/Compose and never imports it.
                "applicable": (bool(getattr(d, "applicable", True))
                               and "flet" not in name),
                "version": d.version or "",
                "hint": _android_hint(d),
                "install": _android_install(d),
                "required": bool(d.required),
            })
        return out

    return _require_engine().run(asyncio.to_thread(_check), timeout=120)


def _android_hint(d) -> str:
    """The dependency's hint, as it is true on Android."""
    name = (d.name or "").lower()
    if "ffmpeg" in name:
        return ("Not available on Android: this app ships no ffmpeg and "
                "cannot install one. Original downloads (mode 1) work "
                "without it; the Fast and Max (MP4) modes cannot.")
    if "cryptg" in name:
        return ("Not shipped on Android: cryptg is a compiled C extension. "
                "Telethon's pure-Python crypto is used instead -- slower, "
                "but it works.")
    if "flet" in name:
        return ("Not used on Android: this app is Kotlin/Compose, not Flet.")
    return d.hint or ""


def _android_install(d) -> str:
    """The install command, or the reason there is none."""
    name = (d.name or "").lower()
    if "ffmpeg" in name:
        return ("No way to install it on Android: the Fast and Max (MP4) "
                "modes are disabled in this app.")
    if "cryptg" in name:
        return ""
    if "flet" in name:
        return ""
    return ""


def get_capabilities() -> dict:
    """What this build can actually do."""
    from .converter import has_ffmpeg
    telethon_version = ""
    try:
        import telethon
        telethon_version = str(getattr(telethon, "__version__", "") or "")
    except Exception:
        telethon_version = ""
    caps = {"platform": "android", "ffmpeg": bool(has_ffmpeg()),
            "modes": ["1"], "version": _app_version(),
            "telethon": telethon_version}
    if caps["ffmpeg"]:
        caps["modes"] = ["1", "2", "3"]
    return caps


def check_for_update() -> dict:
    """Ask the update server what the latest release is.

    The network call happens off the loop (in the loop's executor) and the
    caller waits for the result on its own thread -- never on the UI thread.
    """
    from . import updates as updates_mod

    async def _do() -> dict:
        info = await asyncio.to_thread(updates_mod.check_for_update)
        if info is None:
            return {"ok": False, "error": "Could not reach the update server."}
        current = _app_version()
        return {"ok": True, "version": info.latest_version,
                "url": info.release_url, "current": current,
                "newer": bool(info.is_newer_than(current))}

    return _require_engine().run(_do(), timeout=120)


# ---------------------------------------------------------------------------
# The update download
# ---------------------------------------------------------------------------
#
# The download runs in the BACKGROUND and the UI polls the state, because
# a 25 MB package takes long enough that a UI which waited for it would
# look frozen -- which is the exact bug the user reported about the
# updater: pressing the button and seeing nothing happen.
#
# The state is a plain dict, written by the download thread and read by
# the poller. It is the same shape as the queue's progress: the worker
# never touches a control.
_UPDATE_DOWNLOAD = {
    "status": "idle",     # idle | downloading | done | error
    "progress": 0.0,      # 0..1
    "done": 0,            # bytes so far
    "total": 0,           # bytes expected; 0 when the server did not say
    "detail": "",         # the human line under the bar
    "error": "",
    "path": "",           # where the verified package ended up
    "version": "",
    "name": "",
}


def start_update_download() -> dict:
    """Start downloading this platform's package for the newest release.

    Returns as soon as the download is under way; the progress is polled
    with get_update_download_state(). A second call while one is in
    flight is refused rather than starting a second download.
    """
    from . import updates as updates_mod

    async def _do() -> dict:
        engine = _require_engine()
        if _UPDATE_DOWNLOAD["status"] == "downloading":
            return {"ok": True, "already": True,
                    "version": _UPDATE_DOWNLOAD["version"]}
        info = await asyncio.to_thread(updates_mod.check_for_update)
        if info is None:
            return {"ok": False, "error": "Could not reach the update server."}
        # platform.system() reports "Linux" on Android, so the OS is
        # passed explicitly. Without this the app would look for the
        # desktop .deb and find no Android package at all.
        asset = info.asset_for_platform("android")
        if asset is None:
            return {"ok": False,
                    "error": "This release has no Android package."}
        _UPDATE_DOWNLOAD.update(
            status="downloading", progress=0.0, done=0, total=0, detail="",
            error="", path="", version=info.latest_version, name=asset.name)
        engine.submit(_download_update(info, asset))
        return {"ok": True, "version": info.latest_version, "name": asset.name}

    return _require_engine().run(_do(), timeout=120)


async def _download_update(info, asset) -> None:
    """The download itself, on the engine's loop. Never raises.

    The package lands in the app's OWN storage rather than the user's
    download folder: it is not something the user asked to keep, and the
    app has to be able to hand it to the system package installer, which
    can only read paths the app shares with it.
    """
    from . import updates as updates_mod
    from .telegram_utils import format_bytes

    def _on_progress(done: int, total) -> None:
        _UPDATE_DOWNLOAD["done"] = int(done or 0)
        if total:
            _UPDATE_DOWNLOAD["total"] = int(total)
            _UPDATE_DOWNLOAD["progress"] = min(1.0, done / total)
            _UPDATE_DOWNLOAD["detail"] = (f"{format_bytes(done)} of "
                                          f"{format_bytes(total)}")
        else:
            _UPDATE_DOWNLOAD["detail"] = f"{format_bytes(done)} downloaded"

    try:
        dest_dir = os.path.join(_files_dir, "updates")
        os.makedirs(dest_dir, mode=0o700, exist_ok=True)
        dest = await asyncio.to_thread(
            updates_mod.download_asset, asset, dest_dir,
            info.latest_version,
            os.environ.get("GITHUB_TOKEN") or None,
            progress=_on_progress)
    except Exception as exc:          # noqa: BLE001 - shown to the user
        _UPDATE_DOWNLOAD.update(status="error", error=_sanitize(str(exc)))
        return
    _UPDATE_DOWNLOAD.update(status="done", progress=1.0, path=str(dest))


def get_update_download_state() -> dict:
    """The state of the update download, for the UI to poll."""
    return dict(_UPDATE_DOWNLOAD)


def verify_credentials() -> dict:
    """Check the stored credentials against Telegram."""
    from .config import load_settings

    async def _do() -> dict:
        st = load_settings()
        if not st.api_id or not st.api_hash:
            return {"ok": False, "error": "Save API ID + hash first."}
        engine = _require_engine()
        try:
            c = await engine.get_client()
        except Exception as exc:          # noqa: BLE001 - shown to the user
            return {"ok": False, "error": _sanitize(str(exc))}
        try:
            if await c.is_user_authorized():
                me = await c.get_me()
                name = (getattr(me, "first_name", None)
                        or getattr(me, "username", None) or "your account")
                return {"ok": True, "message": f"Logged in as {name}."}
            return {"ok": True, "message": "Credentials accepted by Telegram. "
                                           "Log in to finish."}
        except Exception as exc:          # noqa: BLE001 - shown to the user
            return {"ok": False, "error": _sanitize(str(exc))}

    return _require_engine().run(_do(), timeout=180)


def login_begin(phone: str) -> dict:
    """Start the login: Telegram sends a code to *phone*.

    A bot token is accepted too -- `tg_client.looks_like_phone` is the
    single router deciding which calling convention telethon gets.
    """
    from . import tg_client

    async def _do() -> dict:
        engine = _require_engine()
        # NOTE: a different name from the parameter on purpose: `phone =
        # phone.strip()` would make the right-hand side read the LOCAL
        # variable (which does not exist yet) and raise UnboundLocalError.
        value = (phone or "").strip()
        if not value:
            return {"ok": False, "state": "error",
                    "error": "Enter your phone number or bot token."}
        try:
            c = await engine.get_client()
            if tg_client.looks_like_phone(value):
                await c.send_code_request(value)
                engine.pending_phone = value
                return {"ok": True, "state": "code"}
            await c.sign_in(bot_token=value)
            engine.pending_phone = ""
            await engine.pin_identity()
            return {"ok": True, "state": "done"}
        except Exception as exc:          # noqa: BLE001 - shown to the user
            return {"ok": False, "state": "error", "error": _sanitize(str(exc))}

    return _require_engine().run(_do(), timeout=180)


def login_code(code: str) -> dict:
    """Submit the login code Telegram sent."""
    from .config import harden_session_files

    async def _do() -> dict:
        engine = _require_engine()
        # A different name from the parameter: `code = code.strip()` would
        # read the not-yet-assigned local and raise UnboundLocalError.
        value = (code or "").strip()
        if not value:
            return {"ok": False, "state": "error", "error": "Enter the code."}
        try:
            c = await engine.get_client()
            try:
                await c.sign_in(phone=engine.pending_phone, code=value)
            except Exception as exc:      # noqa: BLE001 - two-step check
                if _is_password_needed(exc):
                    return {"ok": True, "state": "password"}
                raise
            engine.pending_phone = ""
            await engine.pin_identity()
            harden_session_files()
            return {"ok": True, "state": "done"}
        except Exception as exc:          # noqa: BLE001 - shown to the user
            return {"ok": False, "state": "error", "error": _sanitize(str(exc))}

    return _require_engine().run(_do(), timeout=180)


def login_password(password: str) -> dict:
    """Submit the two-step verification password."""
    from .config import harden_session_files

    async def _do() -> dict:
        engine = _require_engine()
        try:
            c = await engine.get_client()
            await c.sign_in(password=password)
            engine.pending_phone = ""
            await engine.pin_identity()
            harden_session_files()
            return {"ok": True, "state": "done"}
        except Exception as exc:          # noqa: BLE001 - shown to the user
            return {"ok": False, "state": "error", "error": _sanitize(str(exc))}

    return _require_engine().run(_do(), timeout=180)


def _is_password_needed(exc: Exception) -> bool:
    """True when telethon is asking for the 2FA password."""
    try:
        from telethon import errors as terr
        return isinstance(exc, terr.SessionPasswordNeededError)
    except Exception:                     # pragma: no cover - telethon missing
        return "password" in str(exc).lower()


def logout() -> dict:
    """Sign out of Telegram.

    Matches `tg-dl config logout`: the API id/hash belong to the APP and are
    kept, the pinned identity is cleared, and the local session file is
    removed.
    """
    from .config import clear_session, harden_session_files, load_settings, save_settings

    async def _do() -> dict:
        engine = _require_engine()
        server_ok = False
        try:
            c = await engine.get_client()
            await c.log_out()
            server_ok = True
        except Exception:
            pass
        # The pin is what stops a planted session from being adopted, so
        # signing out has to clear it.
        try:
            st = load_settings()
            if getattr(st, "user_id", None):
                st.user_id = None
                save_settings(st)
        except Exception:
            pass
        try:
            clear_session()
        except Exception:
            pass
        try:
            harden_session_files()
        except Exception:
            pass
        engine.pending_phone = ""
        return {"ok": True, "server_ok": bool(server_ok)}

    return _require_engine().run(_do(), timeout=120)


# -- the queue's own actions ------------------------------------------------

def retry_item(item_id: str) -> bool:
    """Put a failed item back in the queue and start the run."""
    engine = _require_engine()
    if not engine.store.get(item_id):
        return False
    engine.store.update(item_id, status="queued", error="")
    engine.start_queue_async()
    return True


def remove_item(item_id: str) -> bool:
    """Remove an item from the queue."""
    engine = _require_engine()
    engine.store.remove(item_id)
    return True


def pause_item(item_id: str) -> bool:
    """Pause a running download.

    The pause is the engine's only stop primitive: it cancels the current
    file and leaves the rest queued. The downloader keeps the partial file
    and its sidecar, so the next run continues from the byte offset.
    """
    engine = _require_engine()
    it = engine.store.get(item_id)
    if not it:
        return False
    if it.status in ("downloading", "converting", "fetching"):
        engine.cancel_batch()
        return True
    return False


def clear_finished() -> bool:
    """Drop the finished and cancelled items."""
    engine = _require_engine()
    engine.store.clear_finished()
    return True


def clear_queue() -> bool:
    """Empty the whole queue."""
    engine = _require_engine()
    engine.store.clear_all()
    return True


def start_queue() -> bool:
    """Resume all / Start: errors go back to queued, then the run starts."""
    engine = _require_engine()
    engine.store.reset_errors_to_queued()
    engine.start_queue_async()
    return True


def cancel_batch() -> bool:
    """Stop the running batch. The current file is cancelled; the rest stay
    queued."""
    _require_engine().cancel_batch()
    return True


def reset_everything() -> bool:
    """The danger zone: sign out, delete the credentials, empty the queue.

    Files already downloaded are not touched.
    """
    from .config import Settings, clear_session, save_settings
    engine = _require_engine()
    engine.cancel_batch()
    try:
        clear_session()
    except Exception:
        pass
    engine.store.clear_all()
    save_settings(Settings())
    return True

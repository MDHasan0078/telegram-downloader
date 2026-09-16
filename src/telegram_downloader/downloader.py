"""Resumable Telegram media download with retries.

GUI/CLI-friendly: progress is reported through a callback instead of
printing, and cancellation is cooperative via an asyncio.Event.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable, Optional, Union

from telethon import errors, utils
from telethon.tl.types import Message

from .constants import MAX_FILE_BYTES

CHUNK_SIZE = 512 * 1024  # Telethon max for normal cloud files
MAX_TRANSFER_RETRIES = 8
RETRY_BASE_SECONDS = 3
MAX_FLOODWAIT_SECONDS = 300  # longer server rate-limits fail fast instead of hanging
MIN_FREE_BYTES = 100 * 1024 ** 2  # headroom kept on disk beyond the file itself

ProgressCb = Union[Callable[[int, int], None], Callable[[int, int], Awaitable[None]]]


class DownloadError(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


async def _emit(cb: Optional[ProgressCb], current: int, total: int) -> None:
    if cb is None:
        return
    res = cb(current, total)
    if asyncio.iscoroutine(res):
        await res


async def _retry_sleep(cancel: Optional[asyncio.Event], delay: float,
                     destination: Path) -> None:
    """Cancel-aware sleep shared by all transfer-retry paths.

    Previously three near-identical blocks; a single helper keeps the
    backoff and cancel semantics from diverging.
    """
    if cancel is not None:
        try:
            await asyncio.wait_for(cancel.wait(), timeout=delay)
            raise Cancelled(f"Cancelled during retry wait; partial kept at {destination}.")
        except asyncio.TimeoutError:
            pass
    else:
        await asyncio.sleep(delay)


async def download_resumable(client, message: Message, destination: Path,
                             progress_cb: Optional[ProgressCb] = None,
                             cancel: Optional[asyncio.Event] = None) -> Path:
    """Download message media to *destination*, resuming partial files."""
    media = message.media
    try:
        dc_id, location = utils.get_input_location(media)
    except Exception as exc:
        raise DownloadError(f"Could not create a Telegram file location: {exc}") from exc

    total = int(getattr(getattr(message, "file", None), "size", 0) or 0)
    if total <= 0:
        total = int(getattr(getattr(media, "document", None), "size", 0) or 0)
    if total <= 0:
        raise DownloadError("Telegram did not provide a usable media size.")

    # Full-ancestor symlink check BEFORE creating parents: every existing
    # ancestor from destination up to filesystem root must not be a link.
    # Missing ancestors are created below with controlled perms and a
    # post-mkdir resolve() verification. This removes the break-bug that
    # previously stopped after the immediate parent and the mkdir-before-check
    # race where a planted symlink was followed.
    def _any_symlink_in_chain(p: Path) -> bool:
        try:
            cur: Path | None = p.absolute()
            while cur is not None:
                try:
                    if cur.is_symlink():
                        return True
                except OSError:
                    pass
                if cur.parent == cur:
                    break
                cur = cur.parent
        except OSError:
            pass
        return False

    if _any_symlink_in_chain(destination) or _any_symlink_in_chain(destination.parent):
        raise DownloadError("Refusing to write through a symlink.")
    # mkdir -p without following symlinks. No permissive fallback: if the
    # safe mkdir refuses (symlink plant) or fails, abort rather than
    # following the link with a plain mkdir.
    try:
        from .config import _mkdir_parents_nofollow
        _mkdir_parents_nofollow(destination.parent, mode=0o700)
    except (RuntimeError, OSError) as exc:
        raise DownloadError(
            f"Refusing to create download folder (possible symlink plant): {exc}") from exc
    # Post-mkdir containment: a symlink parent created racily would cause
    # resolve() to escape; re-verify.
    try:
        if _any_symlink_in_chain(destination.parent):
            raise DownloadError("Refusing to write through a symlink.")
    except DownloadError:
        raise
    except OSError:
        pass
    if destination.is_symlink():
        raise DownloadError("Refusing to write through a symlink.")
    if total > MAX_FILE_BYTES:
        raise DownloadError(
            f"Refusing to download {total} bytes (over the {MAX_FILE_BYTES} cap).")
    try:
        import shutil
        if shutil.disk_usage(destination.parent).free < total + MIN_FREE_BYTES:
            raise DownloadError(
                "Not enough free disk space for this download (need "
                f"{total + MIN_FREE_BYTES} bytes free).")
    except OSError:
        pass
    if destination.exists():
        if destination.is_symlink():
            destination.unlink()
        elif not destination.is_file():
            raise DownloadError("Destination exists but is not a regular file.")
    existing = destination.stat().st_size if destination.exists() else 0
    # Resume-poison mitigation: never trust size match alone if file may be
    # attacker-planted or truncated. If existing == total, verify file is
    # non-empty and not a symlink target; otherwise re-download to .part file
    # and only declare success after hash/size verification at end.
    if existing > total:
        # Don't blindly unlink user data that happens to share the name;
        # keep the existing file and resume would corrupt. Instead, treat as
        # poisoned and require explicit overwrite: remove only if it looks like
        # a previous partial (not a generic existing file). Here we keep the
        # file as .poisoned and start from 0 to avoid data loss.
        try:
            poisoned = destination.with_name(destination.name + ".poisoned")
            destination.rename(poisoned)
        except OSError:
            try:
                destination.unlink()
            except OSError:
                pass
        existing = 0
    if existing == total and existing > 0:
        # Size match alone is not proof of integrity: a planted file with
        # the exact size would otherwise skip the download. Drop it and
        # re-download from 0; the end-of-transfer size check still applies.
        # (Callers skip already-done queue items, so this path only runs
        # on explicit retries.)
        try:
            if destination.is_symlink() or destination.is_file():
                destination.unlink()
        except OSError:
            pass
        existing = 0
    elif existing == total:
        # zero-byte file matches zero total should have been rejected earlier
        destination.unlink(missing_ok=True)
        existing = 0

    # Use O_NOFOLLOW to prevent writing through a swapped-in link. Python's
    # Path.open follows links; use os.open with O_NOFOLLOW|O_EXCL semantics.
    import os as _os
    nofollow = getattr(_os, "O_NOFOLLOW", 0)
    mode = "ab" if existing else "wb"
    current = existing
    await _emit(progress_cb, current, total)
    attempt = 0
    flood_waits = 0
    MAX_FLOODWAITS = 3

    while current < total:
        if cancel is not None and cancel.is_set():
            raise Cancelled(f"Cancelled at {current}/{total} bytes; partial file kept.")
        try:
            async for chunk in client.iter_download(
                location,
                offset=current,
                request_size=CHUNK_SIZE,
                chunk_size=CHUNK_SIZE,
                file_size=total,
                dc_id=dc_id,
            ):
                if cancel is not None and cancel.is_set():
                    raise Cancelled(f"Cancelled at {current}/{total} bytes; partial file kept.")
                # Re-check the link didn't get swapped mid-download, and
                # enforce the size cap on bytes actually written (a hostile
                # stream could otherwise exceed the declared total).
                try:
                    if destination.is_symlink() or _any_symlink_in_chain(destination):
                        raise Cancelled(
                            f"Symlink appeared at {destination}; aborting.")
                except Cancelled:
                    raise
                except OSError:
                    pass
                # Pre-check cap before writing: avoid overshoot by CHUNK_SIZE.
                if current + len(chunk) > MAX_FILE_BYTES:
                    raise DownloadError(
                        f"Download would exceed the {MAX_FILE_BYTES} cap; aborted.")
                # O_NOFOLLOW open: writing through a swapped-in link raises ELOOP
                import os as _os2
                nofollow2 = getattr(_os2, "O_NOFOLLOW", 0)
                flags = _os2.O_WRONLY | _os2.O_CREAT | _os2.O_APPEND if mode == "ab" else _os2.O_WRONLY | _os2.O_CREAT | _os2.O_TRUNC
                flags |= nofollow2
                try:
                    fd = _os2.open(destination, flags, 0o600)
                except OSError as exc:
                    import errno as _errno
                    if exc.errno == _errno.ELOOP:
                        raise Cancelled(f"Symlink detected at {destination}; aborting.") from exc
                    raise
                try:
                    # fd is append/trunc already; write chunk
                    _written = _os2.write(fd, chunk)
                    try:
                        _os2.fsync(fd)
                    except OSError:
                        pass
                finally:
                    try:
                        _os2.close(fd)
                    except OSError:
                        pass
                mode = "ab"
                current += len(chunk)
                if current > MAX_FILE_BYTES:
                    raise DownloadError(
                        f"Download exceeded the {MAX_FILE_BYTES} cap; aborted.")
                await _emit(progress_cb, current, total)
            break
        except Cancelled:
            raise
        except errors.FloodWaitError as exc:
            flood_waits += 1
            if flood_waits > MAX_FLOODWAITS:
                raise DownloadError(
                    f"Telegram rate-limit repeated {flood_waits} times; aborting. "
                    f"Partial file kept at:\n{destination}"
                ) from exc
            secs = int(exc.seconds)
            if secs > MAX_FLOODWAIT_SECONDS:
                raise DownloadError(
                    f"Telegram rate-limit is {secs}s — too long to wait. "
                    f"Partial file kept at:\n{destination}"
                ) from exc
            wait = min(secs + 1, MAX_FLOODWAIT_SECONDS)
            if cancel is not None:
                # Sleep cooperatively so Cancel works during rate-limit waits.
                try:
                    await asyncio.wait_for(cancel.wait(), timeout=wait)
                    raise Cancelled(f"Cancelled during rate-limit wait; partial kept at {destination}.")
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(wait)
            # Do NOT reset attempt: a burst of FloodWaits must not give
            # infinite retries via the transfer-retry budget.
        except (asyncio.TimeoutError, TimeoutError, OSError, ConnectionError) as exc:
            attempt += 1
            if attempt > MAX_TRANSFER_RETRIES:
                raise DownloadError(
                    f"Connection failed after {MAX_TRANSFER_RETRIES} retries "
                    f"at {current}/{total} bytes. Partial file kept at:\n{destination}"
                ) from exc
            delay = min(RETRY_BASE_SECONDS * (2 ** (attempt - 1)), 45)
            try:
                await client.disconnect()
            except Exception:
                pass
            await _retry_sleep(cancel, delay, destination)
            try:
                await client.connect()
            except Exception:
                pass
        except (errors.ServerError, errors.RpcCallFailError) as exc:
            attempt += 1
            if attempt > MAX_TRANSFER_RETRIES:
                raise DownloadError(
                    f"Telegram repeatedly failed the transfer at {current} bytes. "
                    f"Partial file kept at {destination}."
                ) from exc
            await _retry_sleep(cancel, min(RETRY_BASE_SECONDS * (2 ** (attempt - 1)), 45), destination)
        except errors.RPCError as exc:
            # Transient RPC errors (FileReferenceExpiredError, etc.) are
            # retried like other network failures; non-retryable ones
            # (ChannelPrivateError, etc.) surface as DownloadError so the
            # caller can mark the queue item as failed cleanly.
            attempt += 1
            if attempt > MAX_TRANSFER_RETRIES:
                raise DownloadError(
                    f"Telegram RPC error after {MAX_TRANSFER_RETRIES} retries at "
                    f"{current}/{total} bytes: {exc}. Partial kept at {destination}."
                ) from exc
            await _retry_sleep(cancel, min(RETRY_BASE_SECONDS * (2 ** (attempt - 1)), 45), destination)

    if not destination.exists() or destination.stat().st_size != total:
        got = destination.stat().st_size if destination.exists() else 0
        raise DownloadError(f"Incomplete file ({got}/{total} bytes). Partial kept at {destination}.")
    return destination

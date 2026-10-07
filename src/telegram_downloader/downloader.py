"""Resumable Telegram media download with retries.

GUI/CLI-friendly: progress is reported through a callback instead of
printing, and cancellation is cooperative via an asyncio.Event.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
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

# Local write failures that retrying can never fix. These must NOT be treated
# as network errors, or a full disk costs 8 retries, 8 client reconnects and
# ~225s of backoff before finally blaming "the network".
_FATAL_WRITE_ERRNOS = frozenset({
    28,   # ENOSPC  - no space left on device
    122,  # EDQUOT  - disk quota exceeded
    5,    # EIO     - I/O error
    27,   # EFBIG   - file too large
    30,   # EROFS   - read-only filesystem
})


def _media_fingerprint(total: int, dc_id, location) -> str:
    """Identity of the exact media this transfer is for.

    A partial file is only resumable if it provably belongs to *this* media.
    Keying on size alone let two different Telegram messages that share a
    caption+extension splice into one byte-mixed "successful" file.

    Must be STABLE across processes and across two structurally-equal objects.
    `repr()` cannot be used for that: Telethon TL types define no __repr__, so
    it yields `<... object at 0x7f...>` -- the heap address -- and every run
    produced a different digest, so the sidecar never matched and every resume
    silently restarted from zero. `to_dict()` is the serialisable, structural
    form; sort_keys makes the encoding order-independent.
    """
    try:
        payload = location.to_dict() if hasattr(location, "to_dict") else location
        blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8", "replace")
    except Exception:
        # Last resort: still deterministic, just coarser (would let two
        # same-size downloads to the same name share a resume decision).
        blob = json.dumps([total, dc_id, str(type(location).__name__)]).encode()
    return hashlib.sha256(f"{total}|{dc_id}|".encode() + blob).hexdigest()

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
        raise DownloadError("Cannot save here: the folder path is not safe.")
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
            raise DownloadError("Cannot save here: the folder path is not safe.")
    except DownloadError:
        raise
    except OSError:
        pass
    if destination.is_symlink():
        raise DownloadError("Cannot save here: the folder path is not safe.")
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
            raise DownloadError("Cannot save here: the folder path is not safe.")
        elif not destination.is_file():
            raise DownloadError("Destination exists but is not a regular file.")

    # -- Stage into <name>.part with a sidecar proving ownership -------------
    # Writing straight to the final name meant a crash left a truncated file
    # that looked complete, and that resume trusted any bytes it found there.
    # The transfer now only ever appends to a .part whose sidecar vouches for
    # this exact media; the final name is produced by one atomic os.replace.
    part = destination.with_name(destination.name + ".part")
    sidecar = destination.with_name(destination.name + ".part.json")
    fingerprint = _media_fingerprint(total, dc_id, location)

    def _read_sidecar():
        try:
            with open(sidecar, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        if data.get("fingerprint") != fingerprint or data.get("total") != total:
            return None
        return data

    def _write_sidecar() -> None:
        # Atomic + symlink-safe, and 0600: the sidecar lives beside the media.
        from .config import _atomic_write
        _atomic_write(sidecar, json.dumps(
            {"fingerprint": fingerprint, "total": total,
             "name": destination.name}).encode("utf-8"))

    def _discard_partial(reason: str) -> None:
        """Drop an untrustworthy partial, preserving any over-long bytes."""
        if not part.exists():
            return
        if part.stat().st_size > total:
            # Preserve rather than clobber: os.rename overwrites an existing
            # target on POSIX, which destroyed the previously quarantined
            # copy on the second occurrence. Use a unique name.
            quarantined = destination.with_name(
                f"{destination.name}.poisoned.{os.urandom(4).hex()}")
            try:
                os.rename(part, quarantined)
                return
            except OSError:
                # Do NOT fall back to unlink(): that is silent data loss, the
                # exact outcome the quarantine exists to prevent.
                raise DownloadError(
                    f"Refusing to overwrite an oversized partial at {part} "
                    f"and cannot preserve it ({reason}). Move or delete it "
                    f"manually, then retry.")
        try:
            part.unlink()
        except OSError:
            pass
        try:
            sidecar.unlink()
        except OSError:
            pass

    if part.exists():
        meta = _read_sidecar()
        if meta is None:
            # No sidecar, unparseable, or it names different media: we cannot
            # prove those bytes are ours, so they are never appended to.
            _discard_partial("sidecar missing or does not match this media")
            existing = 0
        else:
            existing = part.stat().st_size
    else:
        # A sidecar without its partial is meaningless.
        try:
            sidecar.unlink()
        except OSError:
            pass
        existing = 0

    if existing > total:
        _discard_partial("partial longer than the declared total")
        existing = 0
    if existing == total:
        # Size match alone is not proof of integrity. The sidecar matched, so
        # this really is our own complete transfer staged for publishing.
        try:
            if part.is_symlink() or part.is_file():
                part.unlink()
        except OSError:
            pass
        try:
            sidecar.unlink()
        except OSError:
            pass
        existing = 0

    # Everything from here writes to the staged .part, never the final name.
    final_path = destination
    _stage = part
    destination = part

    # Open ONCE for the whole transfer and verify via fstat on the held fd.
    # Path.open follows links, and the old per-chunk open with O_APPEND let a
    # concurrent process append garbage between chunks while the final size
    # check still passed. A single fd + fstat length verification before every
    # write makes concurrent modification detectable; a path swap mid-transfer
    # just orphans writes to the old inode instead of ever writing through a
    # planted link (our writes never re-open, so ELOOP cannot bite later).
    _os = os
    nofollow = getattr(_os, "O_NOFOLLOW", 0)
    current = existing
    await _emit(progress_cb, current, total)
    attempt = 0
    flood_waits = 0
    # Separate budget. `attempt` is reset on every advancing chunk (so a long
    # but flaky download is not killed by lifetime blips), which means it can
    # never reach MAX_TRANSFER_RETRIES on a peer that keeps sending a little
    # then hanging up -- that case ran forever. stream_ends counts only
    # clean early hangups and is never reset.
    stream_ends = 0
    MAX_FLOODWAITS = 3
    fd = -1

    flags = _os.O_WRONLY | _os.O_CREAT | nofollow
    flags |= _os.O_APPEND if existing else _os.O_TRUNC
    if cancel is not None and cancel.is_set():
        # Nothing has been opened/written yet: a cancelled-before-start must
        # not leave a 0-byte placeholder file behind.
        raise Cancelled(f"Cancelled before download started; nothing written to {destination}.")
    try:
        if not nofollow and destination.is_symlink():
            raise DownloadError(f"Cannot save here: the folder path is not safe at {destination}.")
        fd = _os.open(destination, flags, 0o600)
    except OSError as exc:
        import errno as _errno
        if exc.errno == _errno.ELOOP:
            raise DownloadError(f"Cannot save here: the folder path is not safe at {destination}.") from exc
        raise DownloadError(f"Could not open {destination}: {exc}") from exc
    # Claim the staged partial *before* any byte is written, so an interrupted
    # run leaves a sidecar that proves the .part is resumable.
    # Runs BEFORE the try/finally that owns `fd`, so a failure here must close
    # it explicitly -- it used to leak one fd per failed sidecar write.
    try:
        _write_sidecar()
    except (OSError, RuntimeError) as exc:
        try:
            _os.close(fd)
        except OSError:
            pass
        fd = -1
        raise DownloadError(
            f"Could not write resume sidecar {sidecar}: {exc}") from exc
    try:
        try:
            actual = _os.fstat(fd).st_size
        except OSError:
            actual = -1
        if actual != current:
            # On-disk size drifted between our earlier path.stat() and the
            # open (concurrent writer / poisoned partial). Never trust the
            # tail: truncate the SAME inode and start over from 0.
            try:
                _os.ftruncate(fd, 0)
            except OSError as exc:
                raise DownloadError(
                    f"Could not reset suspicious partial {destination}: {exc}") from exc
            current = 0

        _os.lseek(fd, current, _os.SEEK_SET)
        while current < total:
            if cancel is not None and cancel.is_set():
                raise Cancelled(f"Cancelled at {current}/{total} bytes; partial file kept.")
            # Length drift detection: if the file grew or shrank beneath our fd,
            # somebody else wrote to it — abort rather than produce a file that
            # still passes the final size check.
            try:
                if _os.fstat(fd).st_size != current:
                    # An integrity failure, not a user cancellation: callers
                    # bucket on the exception type, and reporting it as
                    # Cancelled told users they pressed Stop.
                    raise DownloadError(
                        f"Concurrent modification detected at {destination} "
                        f"({_os.fstat(fd).st_size}/{current} bytes); aborting "
                        f"to avoid writing a corrupt file.")
            except (DownloadError, Cancelled):
                raise
            except OSError:
                pass
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
                    # Enforce the cap on bytes actually written (a hostile stream
                    # could otherwise exceed the declared total). The chunk goes
                    # through our single held fd at EOF, so there is no re-open.
                    if current + len(chunk) > MAX_FILE_BYTES:
                        raise DownloadError(
                            f"Download would exceed the {MAX_FILE_BYTES} cap; aborted.")
                    # The write must not be able to fall through to the
                    # network-retry handler below: a local ENOSPC/EROFS is not
                    # a flaky connection and must fail immediately.
                    try:
                        _written = _os.write(fd, chunk)
                    except OSError as exc:
                        if getattr(exc, "errno", None) in _FATAL_WRITE_ERRNOS:
                            raise DownloadError(
                                f"Local write failed ({exc.strerror or exc}); "
                                f"not retrying. Partial file kept at {destination}"
                            ) from exc
                        raise DownloadError(
                            f"Could not write to {destination}: {exc}") from exc
                    if _written != len(chunk):
                        raise DownloadError(
                            f"Short write: {_written} of {len(chunk)} bytes — "
                            f"disk full? Partial file kept at {destination}")
                    current += _written
                    # A chunk that actually advanced the file proves the
                    # connection is healthy, so the consecutive-failure budget
                    # resets here. Without this the 8-retry cap was spent over a
                    # whole session and a long, merely flaky download died.
                    attempt = 0
                    flood_waits = 0
                    await _emit(progress_cb, current, total)
                if current >= total:
                    break
                # Stream ended early without raising (server closed mid-file).
                # The partial is on disk and the loop already supports
                # offset=, so this is a recoverable interruption, not a
                # terminal failure. Counted separately from `attempt`, which
                # is reset by progress and would never trip here.
                stream_ends += 1
                if stream_ends > MAX_TRANSFER_RETRIES:
                    raise DownloadError(
                        f"Telegram ended the stream early {stream_ends} times "
                        f"(last at {current}/{total} bytes). Partial kept at {destination}"
                    )
                await _retry_sleep(
                    cancel, min(RETRY_BASE_SECONDS * (2 ** (stream_ends - 1)), 45),
                    destination)
            except Cancelled:
                raise
            except errors.FloodWaitError as exc:
                flood_waits += 1
                if flood_waits > MAX_FLOODWAITS:
                    raise DownloadError(
                        f"Telegram rate-limit repeated {flood_waits} times; aborting. "
                        f"Partial file kept at:\n{destination}"
                    ) from exc
                try:
                    secs = int(exc.seconds)
                except (TypeError, ValueError):
                    secs = 0
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

        # Verify length from the held fd, never a path stat: the path can be
        # swapped while the fd stays pinned to the original inode we wrote to.
        try:
            final_size = _os.fstat(fd).st_size
        except OSError:
            final_size = -1
        if final_size != total:
            got = final_size if final_size >= 0 else 0
            raise DownloadError(
                f"Incomplete file ({got}/{total} bytes). Partial kept at {destination}.")
        # One flush for the whole transfer: fsync-per-chunk cost ~16k calls on
        # an 8 GiB download, and the length check already covers a torn tail.
        try:
            _os.fsync(fd)
        except OSError:
            pass
        try:
            _os.close(fd)
        except OSError:
            pass
        fd = -1
        # Publish atomically onto the final name. Same directory, so this is a
        # same-filesystem rename: a reader sees either the .part or the
        # finished file, never a half-written "complete" download.
        # os.replace() silently DESTROYS whatever already sits at the final
        # name, so a pre-existing user file that happens to share the
        # download's filename would be lost with no backup. Preserve it first,
        # under a unique name so a second run cannot clobber the first.
        if final_path.exists() and not final_path.is_symlink():
            kept = final_path.with_name(
                f"{final_path.name}.replaced.{os.urandom(4).hex()}")
            try:
                os.replace(final_path, kept)
            except OSError as exc:
                raise DownloadError(
                    f"{final_path} already exists and could not be preserved "
                    f"as {kept.name} ({exc}); refusing to overwrite it. "
                    f"Move or delete it and retry.") from exc
        try:
            _os.replace(_stage, final_path)
        except OSError as exc:
            raise DownloadError(
                f"Downloaded {total} bytes but could not publish to {final_path}: "
                f"{exc}. Complete transfer kept at {_stage}") from exc
        try:
            sidecar.unlink()
        except OSError:
            pass
        return final_path
    finally:
        if fd != -1:
            try:
                _os.close(fd)
            except OSError:
                pass

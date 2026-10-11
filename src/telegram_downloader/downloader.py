"""Resumable Telegram media download with retries.

GUI/CLI-friendly: progress is reported through a callback instead of
printing, and cancellation is cooperative via an asyncio.Event.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat as _stat
import threading
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional, Union

from telethon import errors, utils
from telethon.tl.types import Message

from .constants import (
    MAX_FILE_BYTES,
    PREFIX_HASH_BYTES,
    SIDECAR_HASH_REQUIRED,
    SWEEP_KEEP_PER_NAME,
    SWEEP_MAX_TOTAL,
    SWEEP_MIN_AGE_SECONDS,
    SWEEP_MIN_INTERVAL_SECONDS,
)

CHUNK_SIZE = 512 * 1024  # Telethon max for normal cloud files
MAX_TRANSFER_RETRIES = 8
RETRY_BASE_SECONDS = 3
MAX_FLOODWAIT_SECONDS = 300  # longer server rate-limits fail fast instead of hanging
MIN_FREE_BYTES = 100 * 1024 ** 2  # headroom kept on disk beyond the file itself

# Sidecar keys for the content gate. A sidecar that predates them proves
# PROVENANCE only -- see _partial_prefix_digest and _safe_resume_offset.
_SIDECAR_PREFIX_DIGEST = "prefix_sha256"
_SIDECAR_PREFIX_BYTES = "prefix_bytes"

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


def _partial_prefix_digest(fd: int, size: int) -> str:
    """sha256 over the first *size* bytes of the partial held open at *fd*.

    What this proves, and what it does not, stated plainly: Telegram exposes no
    whole-file checksum for cloud files, so NOTHING here can prove a `.part`
    equals the media. It proves only that the leading bytes are the ones a
    previous run of this transfer wrote. That catches bit rot in the prefix, a
    filesystem that lost or rewrote the head, and any writer that preserves
    length while changing content — all of which previously produced a
    "successful" download full of garbage. Corruption past the first
    PREFIX_HASH_BYTES still passes, and a forger who can write the sidecar can
    write a matching digest: this is a mitigation, not integrity proof.

    Reads through the ALREADY-OPEN transfer fd rather than by path. Hashing by
    path would reopen exactly the inode-swap window the single-held-fd design
    exists to close: the digest would describe whatever the name pointed at,
    not the inode the appends land on.

    *size* is the number of prefix bytes to cover and is clamped to
    PREFIX_HASH_BYTES, so one call costs at most one 64 KiB read no matter what
    a sidecar asks for. The caller records the count alongside the digest,
    which is what makes the check stable as the file grows: the first N bytes
    never change, so a partial that advanced past N between runs still
    verifies against the same digest.

    Raises OSError when fewer than *size* bytes can be read (truncated file,
    unreadable fd). Callers MUST treat that as a verification failure, never
    as a match.
    """
    want = max(0, min(int(size), PREFIX_HASH_BYTES))
    digest = hashlib.sha256()
    if want == 0:
        return digest.hexdigest()
    try:
        saved = os.lseek(fd, 0, os.SEEK_CUR)
        # Always hash from the START. The fd is O_APPEND, so after a write its
        # offset sits at EOF: reading from wherever it happens to be would hash
        # the tail (or nothing at all) and quietly write a digest that can
        # never match.
        os.lseek(fd, 0, os.SEEK_SET)
    except OSError:
        saved = -1  # non-seekable fd: read from wherever it is, do not restore
    try:
        remaining = want
        while remaining > 0:
            block = os.read(fd, remaining)
            if not block:
                raise OSError(
                    f"short read: wanted {want} prefix bytes, got {want - remaining}")
            digest.update(block)
            remaining -= len(block)
    finally:
        if saved >= 0:
            try:
                os.lseek(fd, saved, os.SEEK_SET)
            except OSError:
                pass
    return digest.hexdigest()


# -- retry-artifact sweep -------------------------------------------------
# Every failure mode of "download to an existing name" leaves debris behind:
# the publish path preserves whatever it displaced as "<name>.replaced.<hex>",
# an oversized partial is quarantined as "<name>.poisoned.<hex>", and an item
# that is never retried keeps its "<name>.part" + "<name>.part.json" forever.
# Nothing else in the app knows about those names, so nothing else cleans them:
# a retry loop leaves one full copy of the file per attempt (measured 1000
# bytes x 5 retries) and, with MAX_FILE_BYTES at 8 GiB, a few retries of a
# large file consume multiples of its size.
_ARTIFACT_TAGS = (".replaced.", ".poisoned.")
# Partial + sidecar suffixes are matched exactly (no hex token), so a user file
# that merely contains ".part" in its name is never a candidate.
_PART_SUFFIX = ".part"
_SIDECAR_SUFFIX = ".part.json"

# Absolute paths of .part files this process is writing right now. The sweep
# consults it so it can NEVER delete a live transfer's staging file: mtime is
# only a heuristic (a stalled transfer can go quiet for minutes) and the age
# floor is deliberately hours long. Other processes are covered by the age
# floor instead — their partials are either being written (fresh mtime) or
# genuinely abandoned.
_ACTIVE_PARTS: set = set()
_ACTIVE_PARTS_LOCK = threading.Lock()
# Last sweep time per directory, so a 100-item queue does not rescan the
# directory once per item.
_SWEEP_LAST: dict = {}
_SWEEP_LOCK = threading.Lock()


def _artifact_owner(name: str) -> Optional[str]:
    """Final filename a retry artifact belongs to, or None if it is not one.

    Only names this module itself generates are accepted: the two preserved
    copies carry an 8-hex-character token (os.urandom(4).hex()), and the
    staging pair matches on the exact suffix. A user file that happens to end
    in ".part" or ".replaced.deadbeef"-looking text is treated as ordinary.
    """
    if name.endswith(_SIDECAR_SUFFIX):
        return name[:-len(_SIDECAR_SUFFIX)]
    if name.endswith(_PART_SUFFIX):
        return name[:-len(_PART_SUFFIX)]
    for tag in _ARTIFACT_TAGS:
        idx = name.rfind(tag)
        if idx <= 0:
            continue
        token = name[idx + len(tag):]
        if len(token) == 8 and all(c in "0123456789abcdefABCDEF" for c in token):
            return name[:idx]
    return None


def _register_active_part(path: Path) -> None:
    with _ACTIVE_PARTS_LOCK:
        _ACTIVE_PARTS.add(str(path))


def _forget_active_part(path: Path) -> None:
    with _ACTIVE_PARTS_LOCK:
        _ACTIVE_PARTS.discard(str(path))


def _sweep_artifacts_sync(directory: Path) -> None:
    """Delete old retry debris in *directory*. Best effort, never raises.

    Policy, in order:

    1. Never a `.part` this process is writing (registry above).
    2. Never anything younger than SWEEP_MIN_AGE_SECONDS. The age floor is the
       load-bearing rule, not a nicety: the FIRST artifact of a burst is the
       user's own pre-existing file (publish renames it aside, keeping its
       original mtime), and every later one is a copy of our own output.
       Sorting by recency and keeping the newest copies would delete the
       original; anything under the floor is never touched at all, so "I just
       overwrote a file and want it back" stays true for hours.
    3. Per final name, keep the OLDEST SWEEP_KEEP_PER_NAME and drop the
       surplus newest ones -- those are the duplicates, which re-downloading
       reproduces exactly. (Note this is the opposite of "newest N kept" in
       constants.py: that wording would eat the user's original as soon as a
       third attempt landed. Keeping the oldest is the reading that cannot
       destroy data we did not create.) A ".part" and its ".part.json" count
       as ONE unit: a sidecar without its partial is worthless, so charging
       the 200-byte sidecar against the budget would make an orphaned pair --
       the single biggest thing worth reclaiming -- permanently unsweepable.
    4. Directory-wide cap SWEEP_MAX_TOTAL, again dropping newest surplus
       first.

    Ordering is (mtime, ctime). Publishing renames the displaced file, which
    preserves its mtime but stamps ctime with the moment the artifact was
    created, so ctime breaks mtime ties in true burst order: the first artifact
    of a burst is the user's file, the rest are our own output.

    Symlinks are removed with os.unlink, which removes the LINK and never its
    target's contents, and all metadata comes from lstat so nothing is ever
    followed. Every unlink is individually best-effort: a permission error
    here must never fail a download.
    """
    try:
        now = time.time()
        with _ACTIVE_PARTS_LOCK:
            active = frozenset(_ACTIVE_PARTS)
        candidates = []
        try:
            with os.scandir(directory) as scan:
                for entry in scan:
                    owner = _artifact_owner(entry.name)
                    if owner is None:
                        continue
                    try:
                        # follow_symlinks=False => lstat. Never stats a target.
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if not (_stat.S_ISREG(st.st_mode) or _stat.S_ISLNK(st.st_mode)):
                        continue
                    if entry.path in active:
                        continue
                    if now - st.st_mtime < SWEEP_MIN_AGE_SECONDS:
                        continue  # too young, or an mtime from the future
                    # ctime first: publishing renames the displaced file, which
                    # preserves its mtime but stamps ctime with the moment the
                    # artifact came into existence. So ctime orders a burst
                    # exactly -- first artifact = the user's file -- while
                    # mtime only says how old the *content* is and can be
                    # anything the owner made it.
                    candidates.append((owner, (st.st_ctime, st.st_mtime), entry.path))
        except OSError:
            return

        doomed = set()
        groups: dict = {}
        for owner, order, path in candidates:
            groups.setdefault(owner, {})[path] = order
        for paths in groups.values():
            units = []
            for path in sorted(paths, key=paths.get):
                if path.endswith(_SIDECAR_SUFFIX) and \
                        path[:-len(_SIDECAR_SUFFIX)] + _PART_SUFFIX in paths:
                    continue  # sidecar travels with its .part, not on its own
                units.append(path)
            for path in units[SWEEP_KEEP_PER_NAME:]:
                doomed.add(path)
            # A sidecar never outlives its .part: keep them and discard them
            # together, otherwise a resumed-from-nothing pair would leave a
            # sidecar vouching for a file that is gone.
            for path in paths:
                if path.endswith(_SIDECAR_SUFFIX) and \
                        path[:-len(_SIDECAR_SUFFIX)] + _PART_SUFFIX in doomed:
                    doomed.add(path)

        survivors = sorted((c for c in candidates if c[2] not in doomed),
                           key=lambda c: c[1])
        excess = len(survivors) - SWEEP_MAX_TOTAL
        if excess > 0:
            for _owner, _order, path in survivors[-excess:]:
                doomed.add(path)

        for path in doomed:
            try:
                os.unlink(path)
            except OSError:
                pass
    except Exception:
        # House rule: a sweep failure must never fail a download.
        pass


async def _sweep_retry_artifacts(directory: Path) -> None:
    """Rate-limited entry point called after a successful publish."""
    try:
        key = str(directory)
        now = time.monotonic()
        with _SWEEP_LOCK:
            last = _SWEEP_LAST.get(key)
            if last is not None and now - last < SWEEP_MIN_INTERVAL_SECONDS:
                return
            # Stamped BEFORE the scan, so even a directory that keeps failing
            # cannot turn into one rescan per downloaded item.
            _SWEEP_LAST[key] = now
        # Off the event loop: this walks a user directory (a few thousand
        # entries is milliseconds, a slow network mount is much more) and the
        # GUI must not freeze while it happens.
        await asyncio.to_thread(_sweep_artifacts_sync, directory)
    except Exception:
        pass

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

    def _write_sidecar(fd: int, size: int, pin=None) -> int:
        """Record provenance AND a digest of the first *size* staged bytes.

        *pin* is an already-computed (digest, covered) pair, handed over by
        the content gate so a resume costs ONE bounded read instead of two:
        the bytes were just hashed to verify them, so hashing them again to
        store them is pure waste.

        Returns the number of bytes the recorded digest covers. The count goes
        into the sidecar next to the digest because the file keeps growing:
        the claim-time sidecar covers an empty file, so without the count a
        later resume would hash a longer prefix than the one that was recorded
        and every legitimate resume would look like a mismatch.
        """
        # Atomic + symlink-safe, and 0600: the sidecar lives beside the media.
        from .config import _atomic_write
        if pin is not None:
            digest, covered = pin
        else:
            covered = max(0, min(int(size), PREFIX_HASH_BYTES))
            digest = _partial_prefix_digest(fd, covered)
        _atomic_write(sidecar, json.dumps(
            {"fingerprint": fingerprint, "total": total,
             "name": destination.name,
             _SIDECAR_PREFIX_DIGEST: digest,
             _SIDECAR_PREFIX_BYTES: covered}).encode("utf-8"))
        return covered

    def _safe_resume_offset(fd: int, current: int, meta):
        """(offset it is safe to resume from, reusable digest pin).

        The sidecar proves PROVENANCE; this adds the missing CONTENT gate.
        A `.part` that is the right media but holds rotted bytes passed every
        check there was, because the only content test at the end was
        `final_size != total` and bit rot preserves length.

        Returns `current` only when the recorded digest still describes the
        bytes on disk. Anything else -- mismatch, unreadable prefix, or a
        sidecar with no digest at all -- returns 0. A missing hash is NEVER
        silently treated as a match.
        """
        recorded = meta.get(_SIDECAR_PREFIX_DIGEST) if isinstance(meta, dict) else None
        covered = meta.get(_SIDECAR_PREFIX_BYTES) if isinstance(meta, dict) else None
        legacy = (not isinstance(recorded, str) or not isinstance(covered, int)
                  # bool is an int subclass: True would silently mean 1 byte.
                  or isinstance(covered, bool) or covered < 0
                  or covered > PREFIX_HASH_BYTES)
        if legacy:
            # A sidecar written before prefix hashing existed (or edited into an
            # unusable shape). It still proves this partial belongs to this
            # media, and discarding a legitimately resumable up-to-8 GiB
            # transfer to re-verify a hash nobody ever recorded would punish
            # every user who upgrades mid-download. So per
            # SIDECAR_HASH_REQUIRED: accept it ONCE and re-pin what is actually
            # on disk, so the next resume is checked -- or refuse it and
            # refetch. The caller rewrites the sidecar either way, which is what
            # makes the legacy window a single resume instead of a permanent
            # blind spot.
            return (0, None) if SIDECAR_HASH_REQUIRED else (current, None)
        if covered == 0:
            # Digest of an empty prefix: the claim was written before the first
            # byte landed. Nothing to check, but not a mismatch either -- a
            # partial that has since advanced still has those leading bytes.
            return current, None
        try:
            actual = _partial_prefix_digest(fd, covered)
        except OSError:
            # Unreadable/truncated prefix: unprovable, so restart. Never guess.
            return 0, None
        if actual != recorded:
            return 0, None
        return current, (actual, covered)

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

    meta = None  # sidecar for the bytes we found, if any; drives the content gate
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
    # A complete staged transfer: the sidecar matched, so this really is our
    # own finished download waiting to be published. Deleting it here (as this
    # once did) forced a re-fetch from offset 0 -- so a publish that kept
    # failing (read-only dir, ENOSPC on the directory entry, immutable target)
    # re-pulled the whole file on every retry and made no progress at all,
    # contradicting the "complete transfer kept at ..." promise made by the
    # publish error below. Leave `existing == total` alone and let the publish
    # path run: the fstat integrity gate and the symlink refusal further down
    # still apply, so nothing is skipped except the redundant transfer.
    #
    # `total` is still needed further down, so nothing is reassigned here.

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
    #
    # O_RDWR, not O_WRONLY: the prefix digest has to be read back through the
    # SAME inode the appends land on. Hashing by path would reopen exactly the
    # swap window this fd exists to close -- the digest would describe whatever
    # the name points at, not the file we are about to extend.
    _os = os
    nofollow = getattr(_os, "O_NOFOLLOW", 0)
    current = existing
    await _emit(progress_cb, current, total)
    # `attempt` is the consecutive-failure budget: reset on every advancing
    # chunk (below) so a long but flaky download is not killed by lifetime
    # blips, which also means it can never reach MAX_TRANSFER_RETRIES on a
    # peer that keeps sending a little then hanging up.
    # `flood_waits` is deliberately NOT reset on progress and is NOT folded
    # into `attempt`: MAX_FLOODWAITS has to be a ceiling for the whole
    # transfer. A peer that delivers one chunk and then FloodWaits used to put
    # flood_waits back to 0 on every round, so it never exceeded 1 and the
    # transfer slept up to MAX_FLOODWAIT_SECONDS (300s) per round forever --
    # an unbounded stall that looked like progress in the UI. Progress is not
    # evidence that a rate limit is going away.
    # `stream_ends` counts only clean early hangups and is never reset, for
    # the same reason: it guards the other "always makes a little progress"
    # stall that `attempt` cannot see.
    attempt = 0
    flood_waits = 0
    stream_ends = 0
    MAX_FLOODWAITS = 3
    # How many bytes the sidecar currently pins, so the digest is refreshed
    # exactly once as the partial grows past the hashed prefix.
    prefix_covered = 0
    published = False
    fd = -1

    flags = _os.O_RDWR | _os.O_CREAT | nofollow
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
    # From here until the finally below, this `.part` belongs to a live
    # transfer. The retry-artifact sweep runs in a worker thread during the
    # publish of some *other* item; this is what stops it from unlinking a file
    # we are in the middle of appending to.
    _register_active_part(part)
    try:
        pin = None  # (digest, covered) reused from the content gate, if it verified
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
        elif current > 0:
            # Length agrees, so drift says nothing about CONTENT: a bad
            # sector, a filesystem that lost the head, or any writer that
            # preserves length passes every check above and gets published as a
            # successful download. Only a byte-level gate catches that.
            safe, pin = _safe_resume_offset(fd, current, meta)
            if safe != current:
                # Unprovable, or provably different bytes: discard them and
                # refetch rather than splice a corrupt prefix onto the rest.
                try:
                    _os.ftruncate(fd, 0)
                except OSError as exc:
                    raise DownloadError(
                        f"Could not reset unverified partial {destination}: {exc}") from exc
                current = 0
                pin = None
        # Claim the staged partial *before* any byte is written, so an
        # interrupted run leaves a sidecar that proves the .part is resumable
        # -- and, from here on, that its leading bytes are the ones we wrote.
        # Written AFTER both resume gates so the recorded digest describes the
        # bytes actually kept, and inside the try/finally that owns `fd` so a
        # failure here cannot leak the descriptor (it used to leak one fd per
        # failed sidecar write).
        try:
            prefix_covered = _write_sidecar(fd, current, pin)
        except (OSError, RuntimeError) as exc:
            raise DownloadError(
                f"Could not write resume sidecar {sidecar}: {exc}") from exc

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
                    # `flood_waits` is NOT reset here: MAX_FLOODWAITS is a
                    # ceiling for the transfer, not for a single gap between
                    # chunks. Resetting it meant a peer that served one chunk
                    # per round kept the counter at 1 forever, so the download
                    # rate-limited itself 300s at a time and never gave up.
                    attempt = 0
                    # Pin the prefix digest as soon as the partial is long
                    # enough to have one. The claim written above covers an
                    # empty file, so without this a hard kill (no exit path,
                    # no re-pin) would leave every partial above 64 KiB
                    # permanently unverifiable. Once per transfer: one bounded
                    # read plus one small atomic write.
                    if prefix_covered < PREFIX_HASH_BYTES <= current:
                        try:
                            prefix_covered = _write_sidecar(fd, current)
                        except (OSError, RuntimeError):
                            # Non-fatal here: the exit path re-pins anyway, and
                            # a sidecar we cannot rewrite must not kill a
                            # transfer that is otherwise fine.
                            prefix_covered = PREFIX_HASH_BYTES
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
        published = True
        # Reclaim the disk this retry machinery itself spent. Publishing
        # preserves whatever it displaced as <name>.replaced.<hex>, so a retry
        # loop leaves one full copy of the file behind per attempt -- measured
        # at 1000 bytes x 5 retries, and bounded by nothing at all when
        # MAX_FILE_BYTES is 8 GiB. Swept here because this is the only point
        # where we know the transfer finished and the directory is quiescent
        # for that item.
        await _sweep_retry_artifacts(final_path.parent)
        return final_path
    finally:
        _forget_active_part(part)
        if fd != -1:
            if not published:
                # Leaving with a partial on disk (cancelled, errored, or the
                # transfer is still incomplete): re-pin the sidecar so the NEXT
                # resume verifies the bytes that are actually there instead of
                # the claim-time snapshot. Swallowed deliberately — this runs
                # while another exception is propagating and must not mask it,
                # and failing to record a hash is not a reason to lose a
                # finished download.
                try:
                    _write_sidecar(fd, _os.fstat(fd).st_size)
                except (OSError, RuntimeError, ValueError):
                    pass
            try:
                _os.close(fd)
            except OSError:
                pass

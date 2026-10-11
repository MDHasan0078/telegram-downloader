"""Single source of truth for input/size caps shared by CLI, GUI, store.

Previously each module hardcoded its own copy (100 URLs here, 256 KiB
there) and they drifted. Import from here instead of redefining.
"""
from __future__ import annotations

MAX_URLS = 100                    # max links per run / batch
MAX_URL_LIST_BYTES = 100_000      # max pasted/file text scanned for URLs
MAX_URL_LEN = 2000                # per-URL length cap
MAX_BATCH_BYTES = 256 * 1024      # max .txt batch file size
MAX_FILE_BYTES = 8 * 1024 ** 3    # sanity cap per download (Telegram tops ~4 GiB)
MAX_TITLE_LEN = 140               # display title truncation (CHARACTERS)

# -- filename byte budget -------------------------------------------------
# A filesystem caps one path component at NAME_MAX = 255 BYTES, not
# characters. Titles come from remote captions, and safe_filename() used to
# truncate on `len(name)` (characters) only, so a caption made of 4-byte
# emoji blew straight through the limit: 63 emoji is 63 chars -- under
# MAX_TITLE_LEN, so it was never truncated at all -- but 252 bytes, and the
# caller's extension plus the staging suffix push it to 256+. os.open() then
# raises ENAMETOOLONG (errno 36), the item fails, and because the title is
# re-derived from the same remote caption on every retry it fails identically
# forever. Emoji and CJK captions are ordinary content, so any sender could
# permanently block their own media. Hence a budget in BYTES, derived below.
FS_NAME_MAX_BYTES = 255           # NAME_MAX: the cap is on UTF-8 bytes
MAX_EXT_BYTES = 11                # infer_extension allowlist: "." + 10 [A-Za-z0-9]
MAX_STAGE_SUFFIX_BYTES = 18       # longest self-generated suffix:
                                   #   ".replaced." + 8 hex = 18
                                   #   ".poisoned." + 8 hex = 18
                                   #   (".part" = 5, ".part.json" = 10)
MAX_ESCAPE_PREFIX_BYTES = 1       # leading "_" safe_filename adds for "-" / CON / ...
MAX_SOURCE_TAG_BYTES = 7          # cli/gui convert mode inserts ".source" mid-name
# stem budget: 255 - 11 (ext) - 18 (artifact suffix) - 1 ("_") - 7 (".source")
MAX_TITLE_BYTES = FS_NAME_MAX_BYTES - MAX_EXT_BYTES - MAX_STAGE_SUFFIX_BYTES \
    - MAX_ESCAPE_PREFIX_BYTES - MAX_SOURCE_TAG_BYTES   # == 218

# -- partial integrity ----------------------------------------------------
# NOT YET IMPLEMENTED — deliberately inert. Recorded here so the analysis
# behind it is not lost, and so nobody "fixes" the resume path by referencing
# a constant that does nothing.
#
# Gap: the resume sidecar proves PROVENANCE (which media a `.part` belongs
# to) but nothing binds the partial's actual BYTES. A length-preserving
# corruption — a bad sector, a filesystem that lost the tail, any writer that
# preserves size — resumes and is reported as a successful download, because
# the only content gate is `final_size != total`.
#
# Planned fix: record a digest of the first PREFIX_HASH_BYTES of the staged
# partial in the sidecar, recompute on resume, and restart from 0 on mismatch.
# Telegram exposes no whole-file checksum, so a prefix hash is a pragmatic
# mitigation, not proof of integrity. Costs one extra read of ≤64 KiB.
PREFIX_HASH_BYTES = 64 * 1024
# A sidecar written before this existed still proves PROVENANCE. Restarting
# from 0 would discard a legitimately resumable (up to 8 GiB) transfer to
# re-verify a hash that was never recorded, so the first resume after an
# upgrade proceeds unverified and rewrites the sidecar with a digest of the
# bytes actually on disk; every resume after that is checked. Set True to
# trade that convenience for a guaranteed restart on pre-fix partials.
SIDECAR_HASH_REQUIRED = False

# -- retry-artifact sweep -------------------------------------------------
# NOT YET IMPLEMENTED — deliberately inert. See the note above.
#
# Gap: publishing preserves the file it displaces as "<name>.replaced.<hex>",
# so a retry loop leaves one copy per attempt forever. Measured: five retries
# of a single 1000-byte item left 5000 bytes on disk. With MAX_FILE_BYTES at
# 8 GiB, a few retries of a large file can consume multiples of its size, and
# nothing anywhere sweeps them.
#
# Planned fix: a bounded sweep over the download directory. The age floor
# matters more than the counts — the FIRST artifact of a burst is the user's
# real pre-existing file and the rest are copies of our own output, so
# deleting by recency alone would eat that original. The sweep must also skip
# any `.part` a download is currently writing, never follow symlinks, and
# swallow permission errors rather than failing a download.
SWEEP_MIN_AGE_SECONDS = 6 * 3600  # a preserved copy stays recoverable this long
SWEEP_KEEP_PER_NAME = 2           # newest N artifacts kept per final name
SWEEP_MAX_TOTAL = 200             # hard cap on retained artifacts per directory
SWEEP_MIN_INTERVAL_SECONDS = 300  # at most one directory scan per this often

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
MAX_TITLE_LEN = 140               # display title truncation

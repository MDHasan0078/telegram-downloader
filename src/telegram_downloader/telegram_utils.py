"""Pure helpers: URL parsing, filenames, titles. Fully unit-testable."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Tuple, Union

from .constants import MAX_TITLE_LEN, MAX_URL_LEN, MAX_URL_LIST_BYTES, MAX_URLS

ChatRef = Union[str, int]


def parse_message_url(url: str) -> Tuple[ChatRef, int, Optional[int]]:
    """Return (chat, message_id, thread_id) for supported Telegram links."""
    if not isinstance(url, str):
        raise ValueError("Telegram URL must be text.")
    url = url.strip()
    if len(url) > MAX_URL_LEN:
        raise ValueError("Telegram URL too long.")
    patterns = [
        # t.me/c/<internal id>/<msg> or /<thread>/<msg>  (id capped at 15
        # digits so -100 prefix stays well inside signed 64-bit range)
        (r"^https://(?:t\.me|telegram\.me)/c/(\d{1,15})/(\d{1,10})(?:/(\d{1,10}))?(?:\?.*)?/?$", True),
        # t.me/<username>/<msg> or /<thread>/<msg>
        (r"^https://(?:t\.me|telegram\.me)/([A-Za-z0-9_]{3,32})/(\d{1,10})(?:/(\d{1,10}))?(?:\?.*)?/?$", False),
    ]
    for pattern, is_internal in patterns:
        m = re.match(pattern, url)
        if not m:
            continue
        if is_internal:
            chat: ChatRef = int(f"-100{m.group(1)}")
        else:
            chat = m.group(1)
        if m.group(3):
            return chat, int(m.group(3)), int(m.group(2))
        return chat, int(m.group(2)), None
    raise ValueError(
        "Unsupported Telegram URL. Supported forms:\n"
        "  https://t.me/channel/message_id\n"
        "  https://t.me/channel/thread_id/message_id\n"
        "  https://t.me/c/channel_id/message_id\n"
        "  https://t.me/c/channel_id/thread_id/message_id"
    )


def is_supported_url(url: str) -> bool:
    try:
        parse_message_url(url)
        return True
    except ValueError:
        return False


def safe_filename(name: str) -> str:
    name = str(name)
    # Replace null bytes and control chars first — they can truncate strings
    # in C-based tools (ffmpeg, ffprobe, shell) and confuse argument parsing.
    name = re.sub(r"[\x00-\x1f\x7f]", "_", name)
    # `$` and backticks are shell-special: a crafted title must not be able
    # to smuggle command substitution into a later shell invocation.
    name = re.sub(r"[\\/:*?\"<>|$`]", "_", name)
    name = name.strip().strip(".")
    # Collapse whitespace, cap length for filesystems.
    name = re.sub(r"\s+", " ", name).strip()
    if len(name) > MAX_TITLE_LEN:
        name = name[:MAX_TITLE_LEN].rstrip()
    # Leading dash would make ffmpeg treat the filename as an option.
    if name.startswith("-"):
        name = "_" + name
    # Windows reserved device names (case-insensitive, with or without ext).
    base = name
    if "." in name:
        base = name.rsplit(".", 1)[0]
    upper = base.upper()
    reserved = {"CON", "PRN", "AUX", "NUL"} | {
        f"COM{i}" for i in range(1, 10)
    } | {f"LPT{i}" for i in range(1, 10)}
    if upper in reserved:
        name = "_" + name
    return name or "telegram_video"


def format_bytes(value: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    try:
        n = max(0.0, float(value))
    except (TypeError, ValueError):
        n = 0.0
    for unit in units:
        if n < 1024 or unit == units[-1]:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} {units[-1]}"  # unreachable, keeps type-checkers quiet


def infer_extension(message) -> str:
    ext = ""
    try:
        ext = getattr(message.file, "ext", "") or ""
    except Exception:
        pass
    ext = str(ext)
    # Strict allowlist: a rogue client could otherwise smuggle in
    # separators, null bytes or option-like suffixes via the extension.
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", ext):
        ext = ""
    if not ext:
        return ".mp4" if getattr(message, "video", None) else ".dat"
    if ext.lower() in {".bin", ".unknown"}:
        return ".mp4" if getattr(message, "video", None) else ".dat"
    return ext


def auto_title_from_message(message, chat) -> str:
    """Choose a useful filename from Telegram message metadata."""
    candidates = []
    text = getattr(message, "message", None) or getattr(message, "text", None)
    if text:
        text = re.sub(r"\s+", " ", str(text)).strip()
        if text:
            candidates.append(text)
    try:
        file_name = getattr(message.file, "name", None)
        if file_name:
            candidates.append(Path(file_name).stem)
    except Exception:
        pass
    chat_name = None
    try:
        if hasattr(message, "chat") and message.chat:
            chat_name = getattr(message.chat, "title", None) or getattr(message.chat, "username", None)
    except Exception:
        pass
    chat_name = chat_name or (str(chat) if isinstance(chat, str) else "Telegram")
    msg_id = getattr(message, 'id', 0)
    if msg_id:
        candidates.append(f"{chat_name} - {msg_id}")
    else:
        candidates.append(chat_name)
    title = candidates[0] if candidates else "telegram_video"
    title = re.sub(r"[\n\r\t]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    if len(title) > MAX_TITLE_LEN:
        title = title[:MAX_TITLE_LEN].rstrip()
    return safe_filename(title)


# A link must BE t.me / telegram.me (start-anchored), not merely contain
# it. Bare forms ("t.me/chan/5") get https: normalized; a "foo.t.me" or
# "evil.com/t.me/..." substring never matches.
_TELEGRAM_LINK_RE = re.compile(r"^(?:https://)?(?:t\.me|telegram\.me)(?:/|$)")


def split_urls(raw: str, limit: int = MAX_URLS) -> list[str]:
    """Split pasted text / file content into clean URL list (dedup, order kept).

    Bounded: caps input chars, per-URL length, and total count so a pasted
    megabyte or a giant batch file can't OOM the app or trigger thousands
    of sequential Telegram fetches (FloodWait / ban).
    """
    if not raw:
        return []
    if len(raw) > MAX_URL_LIST_BYTES:
        raw = raw[:MAX_URL_LIST_BYTES]
    seen: set[str] = set()
    out: list[str] = []
    for chunk in re.split(r"[\s,;]+", raw):
        u = chunk.strip().strip("'\"")
        if not u or u in seen:
            continue
        if len(u) > MAX_URL_LEN:
            continue
        if not _TELEGRAM_LINK_RE.match(u):
            continue
        if not u.startswith("https://"):
            u = "https://" + u
        if u in seen:
            continue
        # Validation happens later so the UI shows per-row errors instead
        # of silently dropping lines.
        seen.add(u)
        out.append(u)
        if len(out) >= limit:
            break
    return out

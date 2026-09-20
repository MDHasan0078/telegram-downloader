"""Telethon client helpers shared by CLI and GUI."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Optional

from telethon import TelegramClient, errors

from . import __version__
from .config import session_path_str
from .telegram_utils import auto_title_from_message, infer_extension, parse_message_url


@dataclass
class Preview:
    url: str
    chat: Any
    message_id: int
    thread_id: Optional[int]
    title: str
    ext: str
    size: int = 0
    has_media: bool = True
    error: str = ""


def build_client(api_id: int, api_hash: str) -> TelegramClient:
    from .config import umask_077
    # Build inside umask 077: Telethon touches (creates) the sqlite session
    # during TelegramClient construction under process-global umask, so the
    # whole block must run with private perms — the session file is then
    # owner-only from the start, closing the 0644-until-harden window. Also
    # resolve the session path exactly once (no double call whose return is
    # thrown away).
    with umask_077():
        session = session_path_str()
        client = TelegramClient(
            session,
            api_id,
            api_hash,
            timeout=20,
            request_retries=8,
            connection_retries=8,
            retry_delay=2,
            auto_reconnect=True,
            flood_sleep_threshold=60,
            raise_last_call_error=True,
            device_model="Telegram Downloader",
            app_version=__version__,
        )
    return client


def looks_like_phone(value: str) -> bool:
    """True for phone numbers; bot tokens (e.g. '123:ABC...') return False.

    Telethon needs phone= for numbers and bot_token= for tokens — passing a
    token as phone= fails confusingly, so callers must route explicitly.
    """
    v = (value or "").strip()
    return bool(v) and (v.startswith("+") or v[0].isdigit() and ":" not in v)


async def fetch_preview(client: TelegramClient, url: str) -> Preview:
    """Fetch one URL's metadata (title/size) for the confirm-before-download step."""
    try:
        chat, message_id, thread_id = parse_message_url(url)
    except ValueError as exc:
        return Preview(url=url, chat="", message_id=0, thread_id=None,
                       title="", ext="", has_media=False, error=str(exc))
    try:
        try:
            message = await asyncio.wait_for(client.get_messages(chat, ids=message_id),
                                             timeout=120)
        except errors.FloodWaitError as exc:
            await asyncio.sleep(min(int(exc.seconds) + 1, 300))
            try:
                message = await asyncio.wait_for(client.get_messages(chat, ids=message_id),
                                                 timeout=120)
            except errors.FloodWaitError as exc2:
                return Preview(url=url, chat=chat, message_id=message_id, thread_id=thread_id,
                               title="", ext="", has_media=False,
                               error=f"Telegram rate-limit ({exc2.seconds}s). Try again later.")
        if not message:
            return Preview(url=url, chat=chat, message_id=message_id, thread_id=thread_id,
                           title="", ext="", has_media=False,
                           error="Message not found or no access with this account.")
        if not getattr(message, "media", None):
            return Preview(url=url, chat=chat, message_id=message_id, thread_id=thread_id,
                           title="", ext="", has_media=False,
                           error="Message has no downloadable media (or is only a web preview).")
        title = auto_title_from_message(message, chat)
        ext = infer_extension(message)
        size = int(getattr(getattr(message, "file", None), "size", 0) or 0)
        return Preview(url=url, chat=chat, message_id=message_id, thread_id=thread_id,
                       title=title, ext=ext, size=size, has_media=True)
    except (errors.ChannelPrivateError, errors.ChatAdminRequiredError) as exc:
        return Preview(url=url, chat=chat, message_id=message_id, thread_id=thread_id,
                       title="", ext="", has_media=False,
                       error=f"No access to this chat: {exc}")
    except errors.RPCError as exc:
        return Preview(url=url, chat=chat, message_id=message_id, thread_id=thread_id,
                       title="", ext="", has_media=False, error=f"Telegram error: {exc}")
    except Exception as exc:  # keep one bad URL from killing a batch
        return Preview(url=url, chat=chat, message_id=message_id, thread_id=thread_id,
                       title="", ext="", has_media=False, error=str(exc))


# Interactive login is handled by callers (cli._login_cli / gui.gui_login)
# directly through client.start(phone=|bot_token=). There is no shared
# generic login routine; looks_like_phone is the single router deciding
# which calling convention to use.

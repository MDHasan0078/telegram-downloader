"""Telethon client helpers shared by CLI and GUI."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from telethon import TelegramClient, errors

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
    from .config import session_path_str, umask_077
    # umask 077: Telethon sqlite files are created owner-only from the
    # start, closing the 0644-until-harden window.
    with umask_077():
        session_path_str()
    return TelegramClient(
        session_path_str(),
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
        app_version="0.1.0",
    )


async def ensure_connected(client: TelegramClient) -> None:
    if not client.is_connected():
        await client.connect()


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
            await asyncio.sleep(min(int(exc.seconds) + 1, 60))
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


# Interactive login helpers: GUI passes dialog callbacks, CLI passes input().
# code_cb is kept for API compat but unused (login is via client.start).
PhoneCb = Callable[[], Awaitable[str]]
CodeCb = Callable[[], Awaitable[str]]
PasswordCb = Callable[[], Awaitable[str]]


async def login_interactive(client: TelegramClient, phone_cb: PhoneCb, code_cb: CodeCb,
                            password_cb: PasswordCb,
                            log: Callable[[str], Any] = print) -> str:
    """Perform first-time login. Returns display name. Raises on failure."""
    await ensure_connected(client)
    if await client.is_user_authorized():
        me = await client.get_me()
        return getattr(me, "first_name", None) or getattr(me, "username", None) or "Telegram user"
    phone = (await phone_cb()).strip()
    if not phone:
        raise RuntimeError("Phone number or bot token cannot be empty.")
    try:
        # Single canonical router: phones via phone=, tokens via bot_token=.
        # Never call send_code_request manually (client.start handles it);
        # the old pre-send fired even for bot tokens and spammed SMS.
        if looks_like_phone(phone):
            await client.start(phone=phone)
        else:
            await client.start(bot_token=phone)
    except errors.SessionPasswordNeededError:
        pw = await password_cb()
        try:
            await client.sign_in(password=pw)
        finally:
            # Best-effort: drop the 2FA secret from the local namespace.
            try:
                del pw
            except NameError:
                pass
    me = await client.get_me()
    name = getattr(me, "first_name", None) or getattr(me, "username", None) or "Telegram user"
    from .config import _sanitize_display as _sd
    name = _sd(str(name))
    log(f"Logged in as {name}")
    return name

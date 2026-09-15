"""URL parsing tests (no network)."""
import pytest

from telegram_downloader.telegram_utils import is_supported_url, parse_message_url


def test_public_channel():
    chat, mid, thread = parse_message_url("https://t.me/mychannel/123")
    assert chat == "mychannel" and mid == 123 and thread is None


def test_thread_link():
    chat, mid, thread = parse_message_url("https://t.me/mychannel/244/264")
    assert chat == "mychannel" and mid == 264 and thread == 244


def test_private_c_link():
    chat, mid, thread = parse_message_url("https://t.me/c/1234567890/42")
    assert chat == -1001234567890 and mid == 42 and thread is None


def test_private_thread_link():
    chat, mid, thread = parse_message_url("https://t.me/c/1234567890/244/264")
    assert chat == -1001234567890 and mid == 264 and thread == 244


def test_query_and_slash_tolerant():
    chat, mid, _ = parse_message_url("https://t.me/mychannel/123?single=1/")
    assert mid == 123


def test_unsupported_rejected():
    with pytest.raises(ValueError):
        parse_message_url("https://example.com/not-telegram")
    assert not is_supported_url("https://t.me/")  # too short

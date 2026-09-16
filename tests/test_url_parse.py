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


def test_internal_chat_id_digit_bound():
    # 20-digit internal ids would overflow signed 64-bit; the parser caps
    # the numeric id at 15 digits and must reject anything longer.
    with pytest.raises(ValueError):
        parse_message_url("https://t.me/c/12345678901234567890/1")
    with pytest.raises(ValueError):
        parse_message_url("https://t.me/c/1234567890123456/1")  # 16 digits
    chat, mid, _ = parse_message_url("https://t.me/c/123456789012345/1")  # 15
    assert chat == -100123456789012345 and mid == 1


def test_http_scheme_rejected():
    # https-only: http downgrades must never parse (no silent mixed-content).
    with pytest.raises(ValueError):
        parse_message_url("http://t.me/mychan/1")
    assert not is_supported_url("http://telegram.me/mychan/1")


def test_internal_id_strictly_numeric():
    # An attacker URL like /c/1e10/1 must not slip through as an id.
    with pytest.raises(ValueError):
        parse_message_url("https://t.me/c/1e10/1")
    with pytest.raises(ValueError):
        parse_message_url("https://t.me/c/ab/1")

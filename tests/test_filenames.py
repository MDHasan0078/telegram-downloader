"""Filename / title helper tests."""
from types import SimpleNamespace

from telegram_downloader.telegram_utils import (auto_title_from_message,
                                                format_bytes, safe_filename,
                                                split_urls)


def test_safe_filename_strips_illegal():
    assert safe_filename('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"
    assert safe_filename("   ") == "telegram_video"


def test_format_bytes():
    assert format_bytes(0) == "0.0 B"
    assert format_bytes(1536) == "1.5 KB"


def test_auto_title_prefers_caption():
    msg = SimpleNamespace(message="  Hello   World  ", file=SimpleNamespace(name="vid.mp4"),
                          chat=SimpleNamespace(title="Chan"), id=7)
    assert auto_title_from_message(msg, "Chan") == "Hello World"


def test_auto_title_falls_back_to_chat_id():
    msg = SimpleNamespace(message="", file=SimpleNamespace(name=""),
                          chat=None, id=9)
    assert "9" in auto_title_from_message(msg, "mychan")


def test_split_urls_dedups_and_filters():
    raw = "https://t.me/a/1 https://t.me/a/1\nhttps://t.me/b/2,notaurl"
    assert split_urls(raw) == ["https://t.me/a/1", "https://t.me/b/2"]

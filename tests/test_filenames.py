"""Filename / title helper tests."""
from types import SimpleNamespace

from telegram_downloader.constants import MAX_TITLE_LEN
from telegram_downloader.telegram_utils import (auto_title_from_message,
                                                format_bytes, safe_filename,
                                                split_urls)


def test_safe_filename_strips_illegal():
    assert safe_filename('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"
    assert safe_filename("   ") == "telegram_video"


def test_safe_filename_strips_shell_metachars():
    # `$` and backticks enable command substitution if the title is ever
    # spliced into a shell command (ffmpeg!). They must be scrubbed.
    assert "$" not in safe_filename("pay $5 now")
    assert safe_filename("rm -rf $HOME") == "rm -rf _HOME"
    assert "`" not in safe_filename("echo `id`")


def test_safe_filename_caps_length():
    long = safe_filename("x" * 1000 + ".mp4")
    assert len(long) <= MAX_TITLE_LEN


def test_safe_filename_control_chars():
    assert safe_filename("a\x00b\x1bc\x7fd") == "a_b_c_d"


def test_format_bytes():
    assert format_bytes(0) == "0.0 B"
    assert format_bytes(1536) == "1.5 KB"


def test_format_bytes_clamps_negative():
    # Negative sizes (crafted/corrupt) must render as 0, never "-3.0 KB".
    assert format_bytes(-5) == "0.0 B"
    assert format_bytes("garbage") == "0.0 B"


def test_auto_title_prefers_caption():
    msg = SimpleNamespace(message="  Hello   World  ", file=SimpleNamespace(name="vid.mp4"),
                          chat=SimpleNamespace(title="Chan"), id=7)
    assert auto_title_from_message(msg, "Chan") == "Hello World"


def test_auto_title_falls_back_to_chat_id():
    msg = SimpleNamespace(message="", file=SimpleNamespace(name=""),
                          chat=None, id=9)
    assert "9" in auto_title_from_message(msg, "mychan")


def test_auto_title_caps_length():
    msg = SimpleNamespace(message="word " * 200, file=SimpleNamespace(name=""),
                          chat=None, id=1)
    assert len(auto_title_from_message(msg, "chan")) <= MAX_TITLE_LEN


def test_split_urls_dedups_and_filters():
    raw = "https://t.me/a/1 https://t.me/a/1\nhttps://t.me/b/2,notaurl"
    assert split_urls(raw) == ["https://t.me/a/1", "https://t.me/b/2"]


def test_split_urls_normalizes_bare_tme():
    assert split_urls("t.me/a/1\ntelegram.me/b/2") == [
        "https://t.me/a/1", "https://telegram.me/b/2"]


def test_split_urls_rejects_lookalikes_and_http():
    for bad in ("foo.t.me/a/1",
                "t.me.evil.com/a/1",
                "https://evil.com/t.me/a/1",
                "http://t.me/a/1",
                "ftp://t.me/a/1",
                "file:///etc/passwd"):
        assert bad not in split_urls("t.me/a/1 " + bad), f"{bad} leaked in"

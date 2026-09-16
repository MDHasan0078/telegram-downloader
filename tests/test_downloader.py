"""Download-resume + integrity tests (no Telegram network)."""
import asyncio

import pytest
from types import SimpleNamespace

from telegram_downloader import downloader as dl
from telegram_downloader.downloader import Cancelled, DownloadError, download_resumable


class _FakeMedia:
    pass


class _FakeClient:
    def __init__(self, data: bytes, *, chunks=16):
        self.data = data
        self.chunks = chunks
        self.offsets_used: list[int] = []

    async def iter_download(self, location, *, offset=0,
                            request_size=0, chunk_size=0, file_size=0, dc_id=0):
        self.offsets_used.append(offset)
        while offset < len(self.data):
            yield self.data[offset:offset + self.chunks]
            offset += self.chunks

    async def disconnect(self):
        pass

    async def connect(self):
        pass


def _msg(size: int):
    return SimpleNamespace(media=_FakeMedia(), file=SimpleNamespace(size=size))


@pytest.mark.asyncio
async def test_full_download(monkeypatch, tmp_path):
    data = b"0123456789" * 100  # 1000 bytes
    client = _FakeClient(data)
    monkeypatch.setattr(dl.utils, "get_input_location", lambda m: (1, "loc"))
    dest = tmp_path / "out.bin"
    result = await download_resumable(client, _msg(len(data)), dest)
    assert result == dest
    assert dest.read_bytes() == data


@pytest.mark.asyncio
async def test_resume_from_partial(monkeypatch, tmp_path):
    data = b"A" * 1000
    partial = 400
    client = _FakeClient(data)
    monkeypatch.setattr(dl.utils, "get_input_location", lambda m: (1, "loc"))
    dest = tmp_path / "out.bin"
    dest.write_bytes(data[:partial])
    await download_resumable(client, _msg(len(data)), dest)
    assert dest.read_bytes() == data
    # client must have been told to resume from the partial offset
    assert client.offsets_used == [partial]


@pytest.mark.asyncio
async def test_resume_drift_truncates(monkeypatch, tmp_path):
    import os as _realos
    data = b"X" * 200
    client = _FakeClient(data)
    monkeypatch.setattr(dl.utils, "get_input_location", lambda m: (1, "loc"))
    dest = tmp_path / "out.bin"
    dest.write_bytes(data[:100])  # clean 100-byte partial as seen by stat()
    real_open = _realos.open

    def open_then_grow(path, flags, mode=0o600, *a, **k):
        # Simulate a concurrent append happening AFTER the function's stat
        # but before/at open — the single-fd fstat check must catch the drift
        # (100 -> 103) and truncate back to 0 instead of trusting the tail.
        fd = real_open(path, flags, mode, *a, **k)
        _realos.write(fd, b"BAD")
        return fd

    monkeypatch.setattr(_realos, "open", open_then_grow)
    await download_resumable(client, _msg(len(data)), dest)
    assert dest.read_bytes() == data
    assert client.offsets_used == [0]


@pytest.mark.asyncio
async def test_oversized_existing_file_preserved(monkeypatch, tmp_path):
    data = b"X" * 80
    client = _FakeClient(data)
    monkeypatch.setattr(dl.utils, "get_input_location", lambda m: (1, "loc"))
    dest = tmp_path / "out.bin"
    # existing file is LARGER than the declared media size → resume would
    # corrupt it; it must be preserved as .poisoned, not silently deleted.
    dest.write_bytes(data + b"ATTACKER-GARBAGE")
    await download_resumable(client, _msg(len(data)), dest)
    assert dest.read_bytes() == data
    assert (tmp_path / "out.bin.poisoned").exists()


@pytest.mark.asyncio
async def test_cancel_before_download(monkeypatch, tmp_path):
    cancel = asyncio.Event()
    cancel.set()
    client = _FakeClient(b"ab")
    monkeypatch.setattr(dl.utils, "get_input_location", lambda m: (1, "loc"))
    dest = tmp_path / "out.bin"
    with pytest.raises(Cancelled):
        await download_resumable(client, _msg(2), dest, cancel=cancel)
    # file should NOT exist when cancelled before any bytes.
    assert not dest.exists()


@pytest.mark.asyncio
async def test_symlink_dest_refused(monkeypatch, tmp_path):
    real = tmp_path / "real.bin"
    real.write_bytes(b"xx")
    link = tmp_path / "link.bin"
    link.symlink_to(real)
    monkeypatch.setattr(dl.utils, "get_input_location", lambda m: (1, "loc"))
    with pytest.raises(DownloadError, match="symlink"):
        await download_resumable(_FakeClient(b"xx"), _msg(2), link)


@pytest.mark.asyncio
async def test_too_large_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(dl.utils, "get_input_location", lambda m: (1, "loc"))
    from telegram_downloader.constants import MAX_FILE_BYTES
    with pytest.raises(DownloadError, match="cap"):
        await download_resumable(_FakeClient(b"x"), _msg(MAX_FILE_BYTES + 1),
                                 tmp_path / "big.bin")

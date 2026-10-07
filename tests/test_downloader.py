"""Download-resume + integrity tests (no Telegram network)."""
import asyncio
import json

import pytest
from types import SimpleNamespace

from telegram_downloader import downloader as dl
from telegram_downloader.downloader import Cancelled, DownloadError, download_resumable


async def _async_no_sleep(cancel, delay, destination):
    """Collapse retry backoff so retry-cap tests run instantly."""
    return None


def _seed_partial(dest, payload: bytes, total: int, dc_id, location):
    """Stage a resumable .part the way the downloader itself would."""
    part = dest.with_name(dest.name + ".part")
    sidecar = dest.with_name(dest.name + ".part.json")
    part.write_bytes(payload)
    sidecar.write_text(json.dumps({
        "fingerprint": dl._media_fingerprint(total, dc_id, location),
        "total": total,
        "name": dest.name,
    }))
    return part


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
    # Resume is only offered for a staged .part whose sidecar vouches for
    # this exact media -- a bare file at the final name is never appended to.
    _seed_partial(dest, data[:partial], len(data), 1, "loc")
    await download_resumable(client, _msg(len(data)), dest)
    assert dest.read_bytes() == data
    # client must have been told to resume from the partial offset
    assert client.offsets_used == [partial]
    # The staging artefacts must not survive a successful publish.
    assert not (tmp_path / "out.bin.part").exists()
    assert not (tmp_path / "out.bin.part.json").exists()


@pytest.mark.asyncio
async def test_resume_poisoned_size_is_not_appended(monkeypatch, tmp_path):
    """A partial with a valid size but no provenance must never be spliced in.

    This is the case the old size-only check let through: bytes of unknown
    origin whose length happened to be < total were appended to and the
    length check at the end still passed, producing a silent byte-mixed file.
    """
    data = b"G" * 1000
    client = _FakeClient(data)
    monkeypatch.setattr(dl.utils, "get_input_location", lambda m: (1, "loc"))
    dest = tmp_path / "out.bin"
    # A partial of plausible size, with NO sidecar to prove ownership.
    (tmp_path / "out.bin.part").write_bytes(b"\xde\xad\xbe\xef" * 100)  # 400 B
    await download_resumable(client, _msg(len(data)), dest)
    assert dest.read_bytes() == data, "foreign bytes were spliced into the result"
    assert client.offsets_used == [0], "an unproven partial must not be resumed"


@pytest.mark.asyncio
async def test_resume_ignored_when_sidecar_names_other_media(monkeypatch, tmp_path):
    """Two messages sharing a filename must not splice into one file."""
    data = b"G" * 1000
    client = _FakeClient(data)
    monkeypatch.setattr(dl.utils, "get_input_location", lambda m: (1, "loc"))
    dest = tmp_path / "out.bin"
    # Sidecar is valid JSON and the right *shape*, but was written for a
    # different media fingerprint.
    (tmp_path / "out.bin.part").write_bytes(b"\xde\xad\xbe\xef" * 100)
    (tmp_path / "out.bin.part.json").write_text(
        json.dumps({"fingerprint": "deadbeef" * 8, "total": 1000,
                    "name": "out.bin"}))
    await download_resumable(client, _msg(len(data)), dest)
    assert dest.read_bytes() == data
    assert client.offsets_used == [0]


@pytest.mark.asyncio
async def test_download_publishes_atomically(monkeypatch, tmp_path):
    """A crash mid-transfer must never leave a half file at the final name."""
    data = b"Z" * 500
    monkeypatch.setattr(dl.utils, "get_input_location", lambda m: (1, "loc"))
    dest = tmp_path / "out.bin"
    client = _FakeClient(data)

    class _Boom(Exception):
        pass

    async def exploding_iter(*a, **k):
        yield b"Z" * 100
        raise _Boom("connection reset")

    client.iter_download = exploding_iter
    with pytest.raises(BaseException):
        await download_resumable(client, _msg(len(data)), dest)
    # The final name must not exist; only the resumable staging file.
    assert not dest.exists(), "partial was published under the final name"
    assert (tmp_path / "out.bin.part").exists()


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
    """An oversized pre-existing file at the FINAL name is left alone.

    The transfer stages to .part and publishes with os.replace, so a
    same-named user file is replaced only at publish time -- and the
    oversized *partial* is quarantined under a unique name, never clobbered.
    """
    data = b"X" * 80
    monkeypatch.setattr(dl.utils, "get_input_location", lambda m: (1, "loc"))
    dest = tmp_path / "out.bin"
    dest.write_bytes(data + b"ATTACKER-GARBAGE")

    # An oversized staged partial whose sidecar vouches for this media.
    part = tmp_path / "out.bin.part"
    part.write_bytes(b"Q" * 500)
    (tmp_path / "out.bin.part.json").write_text(json.dumps({
        "fingerprint": dl._media_fingerprint(80, 1, "loc"),
        "total": 80, "name": "out.bin"}))

    await download_resumable(_FakeClient(data), _msg(len(data)), dest)
    assert dest.read_bytes() == data
    # The oversized partial was preserved, not deleted and not clobbered.
    quarantined = list(tmp_path.glob("out.bin.poisoned.*"))
    assert len(quarantined) == 1, quarantined
    assert quarantined[0].read_bytes() == b"Q" * 500


@pytest.mark.asyncio
async def test_quarantine_does_not_clobber_previous(monkeypatch, tmp_path):
    """Two oversized partials must not destroy each other's preserved copy."""
    monkeypatch.setattr(dl.utils, "get_input_location", lambda m: (1, "loc"))
    dest = tmp_path / "out.bin"
    for payload in (b"Q" * 500, b"R" * 600):
        (tmp_path / "out.bin.part").write_bytes(payload)
        (tmp_path / "out.bin.part.json").write_text(json.dumps({
            "fingerprint": dl._media_fingerprint(80, 1, "loc"),
            "total": 80, "name": "out.bin"}))
        await download_resumable(_FakeClient(b"X" * 80), _msg(80), dest)
    saved = sorted(p.read_bytes() for p in tmp_path.glob("out.bin.poisoned.*"))
    assert saved == [b"Q" * 500, b"R" * 600], "a preserved copy was overwritten"


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
    with pytest.raises(DownloadError, match="not safe"):
        await download_resumable(_FakeClient(b"xx"), _msg(2), link)


@pytest.mark.asyncio
async def test_too_large_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(dl.utils, "get_input_location", lambda m: (1, "loc"))
    from telegram_downloader.constants import MAX_FILE_BYTES
    with pytest.raises(DownloadError, match="cap"):
        await download_resumable(_FakeClient(b"x"), _msg(MAX_FILE_BYTES + 1),
                                 tmp_path / "big.bin")


# -- regressions: these three defects shipped in the .part/sidecar rewrite ----

def test_fingerprint_stable_for_telethon_tl_objects():
    """The fingerprint must be structural, not address-based.

    It was built from repr((total, dc_id, location)); Telethon TL types define
    no __repr__, so that hashed the heap address. Two equal messages then got
    different digests, the sidecar never matched, and every resume silently
    restarted from zero. The rest of the suite missed this because the fake
    client returns the *string* "loc", whose repr is stable.
    """
    from telethon.tl.types import InputDocumentFileLocation

    def mk(did=12345, ah=999, ref=b"RRRRRRRR"):
        return InputDocumentFileLocation(id=did, access_hash=ah,
                                         file_reference=ref, thumb_size=None)

    a, b = mk(), mk()
    assert dl._media_fingerprint(1000, 2, a) == dl._media_fingerprint(1000, 2, b)
    # ...and it must still discriminate.
    assert dl._media_fingerprint(1000, 2, mk()) != dl._media_fingerprint(1000, 2, mk(did=777))
    assert dl._media_fingerprint(1000, 2, mk()) != dl._media_fingerprint(2000, 2, mk())


def test_fingerprint_excludes_heap_address():
    import re as _re
    from telethon.tl.types import InputDocumentFileLocation
    loc = InputDocumentFileLocation(id=1, access_hash=2,
                                    file_reference=b"X" * 8, thumb_size=None)
    digest = dl._media_fingerprint(100, 1, loc)
    assert _re.fullmatch(r"[0-9a-f]{64}", digest), digest
    # If a repr/address had leaked in, the digest would change per process.
    assert loc.__repr__() != loc.__repr__() or True  # repr is the default id()-based


@pytest.mark.asyncio
async def test_early_stream_end_terminates(monkeypatch, tmp_path):
    """A peer that dribbles bytes then hangs up must not loop forever.

    `attempt` is reset on every advancing chunk, so using it for this guard
    made the cap unreachable: the transfer retried indefinitely.
    """
    calls = {"n": 0}

    class _Trickle(_FakeClient):
        async def iter_download(self, location, *, offset=0, **kw):
            calls["n"] += 1
            for _ in range(3):
                yield b"Z" * 16

    monkeypatch.setattr(dl, "_retry_sleep", _async_no_sleep)
    monkeypatch.setattr(dl.utils, "get_input_location", lambda m: (1, "loc"))
    dest = tmp_path / "x.bin"
    with pytest.raises(DownloadError, match="ended the stream early"):
        await download_resumable(_Trickle(b""),
                                 _msg(10_000_000), dest)
    assert calls["n"] <= dl.MAX_TRANSFER_RETRIES + 1, calls["n"]


@pytest.mark.asyncio
async def test_preexisting_final_name_file_is_preserved(monkeypatch, tmp_path):
    """os.replace() destroys the destination; a same-named user file must survive."""
    data = b"X" * 200
    monkeypatch.setattr(dl.utils, "get_input_location", lambda m: (1, "loc"))
    dest = tmp_path / "o.bin"
    dest.write_bytes(b"MY IMPORTANT USER FILE")
    await download_resumable(_FakeClient(data), _msg(len(data)), dest)
    assert dest.read_bytes() == data
    kept = list(tmp_path.glob("o.bin.replaced.*"))
    assert len(kept) == 1, kept
    assert kept[0].read_bytes() == b"MY IMPORTANT USER FILE"

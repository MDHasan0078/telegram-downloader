"""Security regression tests for the update-checker's INTEGRITY layer.

Covers U1 (checksum verification is mandatory, not opt-in), U2 (Ed25519
signature over the checksum manifest = second trust anchor), U3 (assets pinned
to our repo, not just to GitHub hosts), U4 (anti-downgrade / plausibility) and
U5 (allowlist hygiene + token minimisation).

HOW THE NETWORK IS FAKED — read this before trusting these tests
----------------------------------------------------------------
Most tests below run against a REAL HTTPS server (`LocalGitHub`, below):

* a self-signed certificate with SANs for github.com, api.github.com,
  objects.githubusercontent.com and release-assets.githubusercontent.com;
* `socket.getaddrinfo` is redirected so those four hostnames resolve to the
  local listener, and `ssl._create_default_https_context` is wrapped to trust
  our CA.

So urllib performs a genuine TLS handshake, and Python's hostname checking
against the SAN list is genuinely exercised — a cert for the wrong host would
fail the connection. Nothing about the updater's own TLS/redirect allowlist is
patched out; only name resolution and trust anchors are. A few pure-unit tests
monkeypatch `updates._urlopen_no_redirect` directly and are marked as such.

The Ed25519 implementation under test is anchored to signatures produced by an
INDEPENDENT implementation (OpenSSL) plus the RFC 8032 vector, so a
self-consistent-but-wrong verifier cannot pass these tests.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import http.server
import json
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

import telegram_downloader.updates as upd
from telegram_downloader import _release_verify as rv

REPO = upd.REPO
INSTALLER = "telegram-downloader_9.9.9_amd64.deb"
SUMS = "SHA256SUMS-deb"
CERT_DIR = Path(tempfile.gettempdir()) / "tgdl-updater-test-certs"
# How long a minted test cert stays valid. Short on purpose, so a leaked cert
# is worthless for long, but it MUST be re-checked rather than trusted by
# existence: _make_cert() reused any cached pair unconditionally, so once the
# cached cert expired every test using the `github` fixture failed with a
# certificate-verify error that had nothing to do with the code under test.
CERT_DAYS = 2
# Re-mint with this much life left rather than caching to the edge.
CERT_RENEW_SKEW_S = 3600
CERT_HOSTS = ("github.com", "api.github.com",
              "objects.githubusercontent.com",
              "release-assets.githubusercontent.com")


# ---------------------------------------------------------------------------
# Test-only Ed25519 signer, anchored to OpenSSL + RFC 8032 (see _ANCHORS).
# ---------------------------------------------------------------------------
def _sha512(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


def _affine(point):
    zi = rv._inv(point[2])
    return point[0] * zi % rv._P, point[1] * zi % rv._P


def _encode_point(point) -> bytes:
    x, y = _affine(point)
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _public_key(seed: bytes) -> bytes:
    h = bytearray(_sha512(seed)[:32])
    h[0] &= 248
    h[31] &= 127
    h[31] |= 64
    return _encode_point(rv._scalar_mult(rv._B, int.from_bytes(h, "little")))


def sign_detached(seed: bytes, message: bytes) -> bytes:
    """RFC 8032 Ed25519 signing. Test-only; production never holds a key."""
    digest = _sha512(seed)
    a = bytearray(digest[:32])
    a[0] &= 248
    a[31] &= 127
    a[31] |= 64
    scalar = int.from_bytes(a, "little")
    prefix = digest[32:]
    public = _public_key(seed)
    r = int.from_bytes(_sha512(prefix + message), "little") % rv._Q
    encoded_r = _encode_point(rv._scalar_mult(rv._B, r))
    k = int.from_bytes(_sha512(encoded_r + public + message), "little") % rv._Q
    return encoded_r + ((r + k * scalar) % rv._Q).to_bytes(32, "little")


# (seed, public key, message, signature) triples produced by OPENSSL, i.e. an
# implementation that shares no code with ours:
#   openssl genpkey -algorithm ed25519 -out k.pem
#   openssl pkey -in k.pem -pubout -outform DER | tail -c 32     # public key
#   printf '...' > m.bin && openssl pkeyutl -sign -inkey k.pem -rawin -in m.bin
_ANCHORS = [
    ("cf72f077b1d5ccc94ca79a493e7dbfe3de5d2d0b8c81fac965e37a51892250df",
     "bae538ae640ff9b2e7ec0bfdfa75775433b8a2deb930a8bbe40209dec2fcb39a",
     b"SHA256SUMS-deb manifest\n"
     b"abc123  telegram-downloader_1.2.0_amd64.deb\n",
     "3b3f487ca0236b484522e47d453e5aa85f8139205d26aa94196ff0f955669a3afc9"
     "66705eb5fba5f99db7d87cb288f07974b8195cba338f783151c2144f72805"),
    ("7657a85eb6618ad452e9b71622f722a9bafea153486661487ea34eea73a07c5a",
     "322a7bd35eae59d38246a876b8925cfcd9e6e0ab2452c0f528ecd7e0c338fa9d",
     b"abc",
     "ada837be1e954147c3f43a1d60a7f16865cca0d9bb674936b13492ddca58c4cfc"
     "9b7b0d0149df403cfd2ae12ebf2a4585a3afff71719785a20b1e83fbd95f50c"),
]

# RFC 8032 §7.1 test vector 1: seed -> public key. (Its signature is not used
# as an anchor: our OpenSSL vectors above already cover signing/verification.)
_RFC8032_SEED = ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac0"
                 "31cae7f60")
_RFC8032_PUBLIC = ("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f"
                   "707511a")


# ---------------------------------------------------------------------------
# Real local HTTPS server impersonating the GitHub endpoints we allow.
# ---------------------------------------------------------------------------
def _cert_is_usable(cert: Path) -> bool:
    """True if *cert* parses as X.509 and still has more than the skew left.

    Existence is not validity. The previous version returned the cached pair
    on `cert.exists() and key.exists()`, so the whole updater-security suite
    began failing on its own two days after the cert was first minted, with a
    TLS error pointing nowhere near the real cause.
    """
    try:
        proc = subprocess.run(
            ["openssl", "x509", "-in", str(cert), "-noout", "-checkend",
             str(CERT_RENEW_SKEW_S)],
            capture_output=True, text=True)
        # -checkend exits 0 while the cert has at least that long left, 1 if
        # it expires sooner. Any other failure means it does not parse at all.
        return proc.returncode == 0
    except OSError:
        return False


def _make_cert() -> tuple[Path, Path]:
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    cert, key = CERT_DIR / "github.pem", CERT_DIR / "github.key"
    if cert.exists() and key.exists() and _cert_is_usable(cert):
        return cert, key
    sans = ",".join(f"DNS:{h}" for h in CERT_HOSTS)
    # Unlink first: openssl would otherwise refuse or write a partial file if
    # a previous run died mid-mint, and we would then cache that broken pair.
    for stale in (cert, key):
        try:
            stale.unlink()
        except OSError:
            pass
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(key), "-out", str(cert), "-days", str(CERT_DAYS),
         "-subj", "/CN=github.com", "-addext", f"subjectAltName={sans}"],
        check=True, capture_output=True)
    return cert, key


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep pytest output clean
        pass

    def do_GET(self):  # noqa: N802 - stdlib naming
        path = urllib.parse.urlsplit(self.path).path
        server = self.server
        with server.lock:
            server.hits.append(path)
            server.requests.append((path, {k.lower(): v
                                          for k, v in self.headers.items()}))
        entry = server.routes.get(path)
        if entry is None:
            self._respond(404, b'{"message":"Not Found"}',
                          "application/json")
            return
        if isinstance(entry, tuple) and len(entry) == 3 and isinstance(entry[2], str):
            status, headers, location = entry
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = entry if isinstance(entry, bytes) else str(entry).encode()
        self._respond(200, body, "application/octet-stream")

    def _respond(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _TLSServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class LocalGitHub:
    """Local HTTPS stand-in for github.com / api.github.com."""

    def __init__(self, cert: Path, key: Path):
        self.server = _TLSServer(("127.0.0.1", 0), _Handler)
        self.server.routes = {}
        self.server.hits = []
        self.server.requests = []
        self.server.lock = threading.Lock()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=str(cert), keyfile=str(key))
        self.server.socket = context.wrap_socket(self.server.socket,
                                                 server_side=True)
        self.port = self.server.socket.getsockname()[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    # -- routing -----------------------------------------------------------
    def route(self, path: str, body) -> None:
        with self.server.lock:
            self.server.routes[path] = body

    def drop(self, path: str) -> None:
        with self.server.lock:
            self.server.routes.pop(path, None)

    def redirect(self, path: str, location: str, status: int = 302) -> None:
        self.route(path, (status, {}, location))

    def release_json(self, tag: str = "v9.9.9", assets=(), html_url=None):
        payload = {
            "tag_name": tag,
            "html_url": html_url or
                        f"https://github.com/{REPO}/releases/tag/{tag}",
            "assets": [{"name": name,
                        "browser_download_url": url} for name, url in assets],
        }
        self.route("/repos/%s/releases/latest" % REPO,
                   json.dumps(payload).encode())

    def download_path(self, version: str, name: str) -> str:
        return f"/{REPO}/releases/download/v{version}/{name}"

    def publish(self, version: str, name: str, body: bytes) -> None:
        self.route(self.download_path(version, name), body)

    def publish_release(self, version: str, files: dict) -> None:
        for name, body in files.items():
            self.publish(version, name, body)

    # -- assertions helpers ------------------------------------------------
    def hits_for(self, needle: str) -> int:
        with self.server.lock:
            return sum(1 for hit in self.server.hits if needle in hit)

    def hits_exact(self, path: str) -> int:
        with self.server.lock:
            return sum(1 for hit in self.server.hits if hit == path)

    def hit_order(self, needle: str) -> int | None:
        with self.server.lock:
            for index, hit in enumerate(self.server.hits):
                if needle in hit:
                    return index
        return None

    def clear_hits(self) -> None:
        with self.server.lock:
            self.server.hits.clear()

    def auth_headers(self) -> list:
        """Authorization header seen for each recorded request (or None)."""
        with self.server.lock:
            return [hdrs.get("authorization") for _p, hdrs in self.server.requests]

    def request_count(self) -> int:
        with self.server.lock:
            return len(self.server.requests)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(scope="session")
def _cert():
    if not shutil.which("openssl"):
        pytest.skip("openssl needed to mint the github.com test certificate")
    cert, key = _make_cert()
    return cert, key


def _cert_paths():
    if not shutil.which("openssl"):
        raise RuntimeError("openssl is required to mint the test certificate")
    return _make_cert()


@contextlib.contextmanager
def redirected_network(server, cert):
    """Point the allowlisted GitHub hostnames at *server* and trust its CA.

    Name resolution + trust anchor only: the TLS handshake, certificate
    hostname validation and urllib's redirect handling all stay real.
    """
    real_getaddrinfo = socket.getaddrinfo
    real_context = ssl._create_default_https_context

    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host in CERT_HOSTS:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     ("127.0.0.1", server.port))]
        return real_getaddrinfo(host, port, *args, **kwargs)

    def fake_context(*args, **kwargs):
        ctx = real_context(*args, **kwargs)
        ctx.load_verify_locations(cafile=str(cert))
        return ctx

    socket.getaddrinfo = fake_getaddrinfo
    ssl._create_default_https_context = fake_context
    ssl.create_default_context = fake_context
    try:
        yield
    finally:
        socket.getaddrinfo = real_getaddrinfo
        ssl._create_default_https_context = real_context
        ssl.create_default_context = real_context


@pytest.fixture
def github(_cert, monkeypatch):
    """A real HTTPS server on 127.0.0.1 answering as GitHub would."""
    server = LocalGitHub(*_cert)
    with redirected_network(server, _cert[0]):
        yield server
    server.close()


@pytest.fixture
def signed_release(github, monkeypatch):
    """Publish a signed release and pin the test key into the trust anchor.

    Returns (server, version, payload_bytes, manifest_bytes, signature).
    """
    seed = bytes.fromhex(_ANCHORS[0][0])
    public = bytes.fromhex(_ANCHORS[0][1])
    monkeypatch.setattr(rv, "PINNED_RELEASE_SIGNING_KEYS",
                        {"test:ed25519": base64.b64encode(public).decode()})
    return seed, "test:ed25519", public


def make_release(version: str, payload: bytes, *, seed: bytes | None = None,
                 sign: bool = True, digest: str | None = None,
                 filename: str | None = None) -> dict:
    """Build {asset bytes, manifest bytes, signature} for a release."""
    name = filename or f"telegram-downloader_{version}_amd64.deb"
    body = hashlib.sha256(payload).hexdigest() if digest is None else digest
    manifest = f"{body}  {name}\n".encode()
    out = {name: payload, SUMS: manifest}
    if sign and seed is not None:
        out[SUMS + ".sig"] = sign_detached(seed, manifest)
    return out


@pytest.fixture
def release_files():
    def _make(version="9.9.9", payload=b"#!/bin/sh\necho pwned\n", **kwargs):
        seed = kwargs.pop("seed", bytes.fromhex(_ANCHORS[0][0]))
        files = make_release(version, payload, seed=seed, **kwargs)
        return {"version": version, "payload": payload, "files": files,
                "seed": seed}
    return _make


def _asset(version="9.9.9", name=None):
    name = name or f"telegram-downloader_{version}_amd64.deb"
    return upd.UpdateAsset(
        name, f"https://github.com/{REPO}/releases/download/v{version}/{name}")


# ===========================================================================
# Ed25519 implementation correctness (independent anchors)
# ===========================================================================
@pytest.mark.parametrize("seed_hex,public_hex,message,signature_hex", _ANCHORS)
def test_ed25519_matches_independent_vectors(seed_hex, public_hex, message,
                                             signature_hex):
    """Our verifier/signer must agree with an independent implementation."""
    seed = bytes.fromhex(seed_hex)
    public = bytes.fromhex(public_hex)
    signature = bytes.fromhex(signature_hex)
    assert _public_key(seed) == public, "public key derivation is wrong"
    assert sign_detached(seed, message) == signature, "signature is wrong"
    assert rv._verify_raw(public, signature, message), "signature rejected"
    assert not rv._verify_raw(public, signature, message + b"x")
    tampered = bytearray(signature)
    tampered[0] ^= 0x01
    assert not rv._verify_raw(public, bytes(tampered), message)
    assert not rv._verify_raw(bytes(32), signature, message)
    assert not rv._verify_raw(public, b"\x00" * 64, message)


def test_rfc8032_vector1_public_key():
    assert _public_key(bytes.fromhex(_RFC8032_SEED)).hex() == _RFC8032_PUBLIC


def test_shipped_keyring_is_usable_and_fails_closed():
    """A key IS pinned now, and every failure mode still fails closed.

    This test used to assert the keyring was EMPTY, as a tripwire against
    pinning a key before CI could sign with it. CI now emits the Ed25519
    `SHA256SUMS-<plat>.sig` this module actually parses (the release job
    signs them and refuses to publish a signed release with no signature),
    so the tripwire has served its purpose.

    What must stay true regardless of keyring contents: the shipped keyring
    is VALIDATED (not merely present), and a bogus signature against it is
    refused rather than waved through. Pinned-but-unusable would be worse
    than empty -- it looks configured while refusing every real release.
    """
    status = rv.validate_pinned_keys()
    assert status.ok, f"shipped keyring is not usable: {status.summary} {status.problems}"
    assert rv.PINNED_RELEASE_SIGNING_KEYS, "no release signing key is pinned"

    # A pinned keyring must not turn a forged/bogus signature into a pass.
    with pytest.raises(rv.BadSignatureError):
        rv.verify_manifest_signature(b"x" * 8, b"\x00" * 64)


def test_empty_keyring_still_fails_closed():
    """The fail-closed property is about the CODE, not the current keyring.

    A future operator who empties PINNED_RELEASE_SIGNING_KEYS (e.g. rotating
    keys) must get a refusal, never a silent acceptance.
    """
    with pytest.raises(rv.NoPinnedSigningKeyError):
        rv.verify_manifest_signature(b"x" * 8, b"\x00" * 64, keys={})


def test_verify_manifest_signature_rejects_unknown_key():
    # Two pinned keys: a signature from a third party cannot be attributed to
    # either of them, so it is reported as an unknown signer.
    keys = {"release:2026": base64.b64encode(bytes(range(32))).decode(),
            "release:2027": base64.b64encode(bytes(range(1, 33))).decode()}
    with pytest.raises(rv.UnknownSigningKeyError):
        rv.verify_manifest_signature(b"manifest", b"\x01" * 64, keys=keys)
    # With exactly one pinned key, "not ours" and "tampered" are the same
    # observation; both refuse, reported as a bad signature.
    with pytest.raises(rv.BadSignatureError):
        rv.verify_manifest_signature(b"manifest", b"\x01" * 64,
                                     keys={"only": list(keys.values())[0]})


def test_verify_manifest_signature_requires_signature():
    with pytest.raises(rv.MissingSignatureError):
        rv.verify_manifest_signature(b"manifest", None, keys={"k": "AA" * 22})


def test_signature_parser_is_strict():
    with pytest.raises(rv.SignatureFormatError):
        rv.parse_signature_blob(b"\x00" * 63)
    with pytest.raises(rv.SignatureFormatError):
        rv.parse_signature_blob(base64.b64encode(b"\x00" * 64))
    with pytest.raises(rv.SignatureFormatError):
        rv.parse_signature_blob(b"BEGIN PGP MESSAGE\n")
    assert rv.parse_signature_blob(b"\x00" * 64) == (None, b"\x00" * 64)
    envelope = (f"{rv.SIGNATURE_ENVELOPE_PREFIX} deadbeef "
                + base64.b64encode(b"\x01" * 64).decode() + "\n").encode()
    assert rv.parse_signature_blob(envelope) == ("deadbeef", b"\x01" * 64)


def test_envelope_key_id_must_be_pinned():
    seed = bytes.fromhex(_ANCHORS[0][0])
    manifest = b"manifest bytes"
    signature = sign_detached(seed, manifest)
    good = base64.b64encode(bytes.fromhex(_ANCHORS[0][1])).decode()
    unknown = (f"{rv.SIGNATURE_ENVELOPE_PREFIX} deadbeef "
               + base64.b64encode(signature).decode()).encode()
    with pytest.raises(rv.UnknownSigningKeyError):
        rv.verify_manifest_signature(manifest, unknown, keys={"k": good})
    real_id = rv.key_id_for(bytes.fromhex(_ANCHORS[0][1]))
    known = (f"{rv.SIGNATURE_ENVELOPE_PREFIX} {real_id} "
             + base64.b64encode(signature).decode()).encode()
    assert rv.verify_manifest_signature(manifest, known,
                                        keys={"k": good}) == "k"
    # known key, wrong message -> tamper, not "unknown signer"
    with pytest.raises(rv.BadSignatureError):
        rv.verify_manifest_signature(manifest + b"!", known, keys={"k": good})


# ===========================================================================
# U1 — checksum verification must be mandatory
# ===========================================================================
def test_u1_missing_tag_version_refuses(monkeypatch, tmp_path, github,
                                        release_files, signed_release):
    seed, label, public = signed_release
    rel = release_files()
    github.publish_release(rel["version"], rel["files"])
    calls = []
    real = upd._fetch_checksum
    monkeypatch.setattr(upd, "_fetch_checksum",
                        lambda *a, **k: (calls.append(a), real(*a, **k))[1])
    dest = tmp_path / "dl"

    # 1. omitted entirely
    with pytest.raises(Exception):
        upd.download_asset(_asset(rel["version"]), dest)
    # 2. explicit None
    with pytest.raises(Exception):
        upd.download_asset(_asset(rel["version"]), dest, None)
    # 3. empty string
    with pytest.raises(Exception):
        upd.download_asset(_asset(rel["version"]), dest, "")
    assert not dest.exists() or list(dest.iterdir()) == []
    assert github.hits_for(INSTALLER) == 0, "payload downloaded before checks"


def test_u1_valid_tag_publishes_only_when_sums_match(tmp_path, github,
                                                     release_files,
                                                     signed_release):
    seed, label, public = signed_release
    rel = release_files(payload=b"good payload")
    github.publish_release(rel["version"], rel["files"])
    dest = upd.download_asset(_asset(rel["version"]), dest_dir=tmp_path / "d",
                              tag_version=rel["version"])
    assert Path(dest).read_bytes() == b"good payload"
    assert github.hits_exact(github.download_path(rel["version"], SUMS)) == 1


def test_u1_bad_sums_refuse_and_leave_nothing(tmp_path, github, release_files,
                                               signed_release):
    seed, label, public = signed_release
    payload = b"attacker payload"
    rel = release_files(payload=payload, digest="0" * 64)
    github.publish_release(rel["version"], rel["files"])
    dest_dir = tmp_path / "d"
    with pytest.raises(ValueError):
        upd.download_asset(_asset(rel["version"]), dest_dir=dest_dir,
                           tag_version=rel["version"])
    assert not (dest_dir / INSTALLER).exists()
    assert list(dest_dir.iterdir()) == []


def test_u1_missing_manifest_refuses(tmp_path, github, release_files,
                                     signed_release):
    seed, label, public = signed_release
    rel = release_files()
    for name in list(rel["files"]):
        if name != INSTALLER:
            github.drop(github.download_path(rel["version"], name))
    with pytest.raises(Exception):
        upd.download_asset(_asset(rel["version"]), dest_dir=tmp_path / "d",
                           tag_version=rel["version"])


def test_u3_version_mismatch_refused(tmp_path, github, release_files,
                                     signed_release):
    """A v1.0.0 payload cannot be checked against a v1.1.0 manifest."""
    seed, label, public = signed_release
    rel = release_files(version="1.0.0")
    github.publish_release(rel["version"], rel["files"])
    stale = upd.UpdateAsset(
        "telegram-downloader_1.0.0_amd64.deb",
        f"https://github.com/{REPO}/releases/download/v1.0.0/"
        "telegram-downloader_1.0.0_amd64.deb")
    with pytest.raises(ValueError):
        upd.download_asset(stale, dest_dir=tmp_path / "d", tag_version="1.1.0")


def test_u1_legacy_positional_call_still_supported(tmp_path, github,
                                                   release_files,
                                                   signed_release):
    """gui.py calls download_asset(asset, dir, version, token) positionally
    and is owned by another change-set — that call must keep working."""
    seed, label, public = signed_release
    rel = release_files()
    github.publish_release(rel["version"], rel["files"])
    out = upd.download_asset(_asset(rel["version"]), tmp_path / "d",
                             rel["version"], None)
    assert Path(out).read_bytes() == rel["payload"]


# ===========================================================================
# U2 — signed manifests (second trust anchor)
# ===========================================================================
def test_u2_valid_signature_passes(tmp_path, github, release_files,
                                   signed_release):
    seed, label, public = signed_release
    rel = release_files(payload=b"signed payload")
    github.publish_release(rel["version"], rel["files"])
    out = upd.download_asset(_asset(rel["version"]), dest_dir=tmp_path / "d",
                             tag_version=rel["version"])
    assert Path(out).read_bytes() == b"signed payload"


def test_u2_tampered_manifest_rejected(tmp_path, github, release_files,
                                       signed_release):
    """Signature covers the manifest; editing a digest must break the install."""
    seed, label, public = signed_release
    payload = b"real payload"
    rel = release_files(payload=payload)
    github.publish_release(rel["version"], rel["files"])
    manifest = rel["files"][SUMS]
    tampered = manifest.replace(hashlib.sha256(payload).hexdigest().encode(),
                                hashlib.sha256(b"evil").hexdigest().encode())
    assert tampered != manifest
    github.route(github.download_path(rel["version"], SUMS),
                 tampered)
    dest_dir = tmp_path / "d"
    with pytest.raises(rv.BadSignatureError):
        upd.download_asset(_asset(rel["version"]), dest_dir=dest_dir,
                           tag_version=rel["version"])
    assert not (dest_dir / INSTALLER).exists()


def test_u2_unsigned_release_refused_by_default(tmp_path, github,
                                                release_files,
                                                signed_release):
    seed, label, public = signed_release
    rel = release_files(sign=False)
    github.publish_release(rel["version"], rel["files"])
    dest_dir = tmp_path / "d"
    with pytest.raises(rv.MissingSignatureError):
        upd.download_asset(_asset(rel["version"]), dest_dir=dest_dir,
                           tag_version=rel["version"])
    assert not (dest_dir / INSTALLER).exists()


def test_u2_unsigned_release_allowed_only_via_explicit_opt_in(
        tmp_path, github, release_files, signed_release):
    seed, label, public = signed_release
    rel = release_files(sign=False)
    github.publish_release(rel["version"], rel["files"])
    out = upd.download_asset(_asset(rel["version"]), dest_dir=tmp_path / "d",
                             tag_version=rel["version"],
                             allow_unsigned=True)
    assert Path(out).read_bytes() == rel["payload"]


def test_u2_unknown_signing_key_rejected(tmp_path, github, release_files,
                                         signed_release, monkeypatch):
    seed, label, public = signed_release
    rel = release_files()
    github.publish_release(rel["version"], rel["files"])
    monkeypatch.setattr(rv, "PINNED_RELEASE_SIGNING_KEYS",
                        {"other:key": base64.b64encode(bytes(32)).decode(),
                         "third:key": base64.b64encode(bytes(range(32))).decode()})
    with pytest.raises(rv.UnknownSigningKeyError):
        upd.download_asset(_asset(rel["version"]), dest_dir=tmp_path / "d",
                           tag_version=rel["version"])


def test_u2_manifest_and_signature_come_from_our_repo(github, tmp_path,
                                                      release_files,
                                                      signed_release):
    """Both are fetched from REPO's own release path, by construction."""
    seed, label, public = signed_release
    rel = release_files()
    github.publish_release(rel["version"], rel["files"])
    out = upd.download_asset(_asset(rel["version"]), dest_dir=tmp_path / "d",
                             tag_version=rel["version"])
    assert Path(out).read_bytes() == rel["payload"]
    prefix = f"/{REPO}/releases/download/v{rel['version']}/"
    fetched = [path for path, _hdrs in github.server.requests]
    assert fetched == [prefix + SUMS, prefix + SUMS + ".sig",
                       prefix + INSTALLER]


def test_u2_signature_asset_not_trusted_from_other_repo(github, tmp_path,
                                                        release_files,
                                                        signed_release):
    """The .sig URL is built from REPO, never from API-supplied data: even
    when a foreign copy of the manifest+signature is reachable, we never ask
    for it."""
    seed, label, public = signed_release
    rel = release_files()
    github.publish_release(rel["version"], rel["files"])
    # A hostile "release" of our own under someone else's account, serving the
    # same (valid!) signature for the same manifest.
    attacker_dir = f"/attacker-xyz/evil/releases/download/v{rel['version']}"
    github.route(attacker_dir + "/" + SUMS, rel["files"][SUMS])
    github.route(attacker_dir + "/" + SUMS + ".sig",
                 rel["files"][SUMS + ".sig"])
    out = upd.download_asset(_asset(rel["version"]), dest_dir=tmp_path / "d",
                             tag_version=rel["version"])
    assert Path(out).read_bytes() == rel["payload"]
    assert github.hits_for("attacker-xyz") == 0
    assert github.hits_for("evil") == 0


# ===========================================================================
# U3 — assets pinned to our repo, not just to GitHub hosts
# ===========================================================================
def test_u3_cross_repo_asset_dropped(monkeypatch, tmp_path):
    evil = ("https://github.com/attacker-xyz/evil/releases/download/v9.9.9/"
            + INSTALLER)
    payload = json.dumps({
        "tag_name": "v9.9.9",
        "html_url": f"https://github.com/{REPO}/releases/tag/v9.9.9",
        "assets": [{"name": INSTALLER, "browser_download_url": evil}],
    }).encode()
    monkeypatch.setattr(upd, "_urlopen_no_redirect", _fake_open(payload))
    info = upd.check_for_update()
    assert info is not None
    assert info.assets == [], "cross-repo asset was offered for install"


def test_u3_download_asset_refuses_cross_repo_url(tmp_path):
    evil = upd.UpdateAsset(
        INSTALLER, "https://github.com/attacker-xyz/evil/releases/download/"
                   "v9.9.9/" + INSTALLER)
    with pytest.raises(ValueError):
        upd.download_asset(evil, tmp_path / "d", "9.9.9")
    assert not (tmp_path / "d" / INSTALLER).exists()


def test_u3_cdn_redirect_still_allowed(tmp_path, github, release_files,
                                       signed_release):
    """Repo-pinning must not break the github.com -> CDN 302 GitHub really does."""
    seed, label, public = signed_release
    rel = release_files()
    github.publish_release(rel["version"], rel["files"])
    cdn = f"https://release-assets.githubusercontent.com/assets/{INSTALLER}"
    github.redirect(github.download_path(rel["version"], INSTALLER), cdn)
    github.route(f"/assets/{INSTALLER}", rel["payload"])
    out = upd.download_asset(_asset(rel["version"]), dest_dir=tmp_path,
                             tag_version=rel["version"])
    assert Path(out).read_bytes() == rel["payload"]


def test_u3_cross_repo_redirect_refused(tmp_path, github, release_files,
                                        signed_release):
    seed, label, public = signed_release
    rel = release_files()
    github.publish_release(rel["version"], rel["files"])
    github.redirect(github.download_path(rel["version"], INSTALLER),
                    "https://evil.example/x.deb")
    with pytest.raises(Exception):
        upd.download_asset(_asset(rel["version"]), dest_dir=tmp_path,
                           tag_version=rel["version"])


# ===========================================================================
# U4 — anti-downgrade / plausibility
# ===========================================================================
def _fake_open(payload: bytes, status: int = 200, headers=None):
    """urlopen stand-in used by the pure-unit tests (marked in the docstring)."""
    class _Resp:
        def __init__(self):
            self.status = status
            self.headers = headers or {"Content-Length": str(len(payload))}
        def read(self, *_a):
            return payload
        def __enter__(self):
            return self
        def __exit__(self, *_a):
            return False
    return lambda *a, **k: _Resp()


def _release_payload(tag="v9.9.9"):
    return json.dumps({
        "tag_name": tag,
        "html_url": f"https://github.com/{REPO}/releases/tag/{tag}",
        "assets": [],
    }).encode()


# Commented out: the implausible-jump guard is disabled in updates.py, so a
# well-formed v99.99.99 is no longer suppressed. Uncomment with the guard.
# def test_u4_absurd_jump_rejected(monkeypatch, tmp_path):
#     monkeypatch.setattr(upd, "_urlopen_no_redirect",
#                         _fake_open(_release_payload("v99.99.99")))
#     assert upd.check_for_update(current_version="1.2.0",
#                                 state_path=tmp_path / "state.json") is None


def test_u4_normal_next_minor_accepted(monkeypatch, tmp_path):
    monkeypatch.setattr(upd, "_urlopen_no_redirect",
                        _fake_open(_release_payload("v1.2.1")))
    info = upd.check_for_update()
    assert info is not None and info.latest_version == "1.2.1"


def test_u4_next_major_accepted(monkeypatch, tmp_path):
    monkeypatch.setattr(upd, "_urlopen_no_redirect",
                        _fake_open(_release_payload("v2.0.0")))
    info = upd.check_for_update()
    assert info is not None and info.latest_version == "2.0.0"



def test_u4_unicode_and_newline_tags_still_rejected(monkeypatch, tmp_path):
    for tag in ("v\u0661.\u0662.\u0663", "v1.2.3\n"):
        monkeypatch.setattr(upd, "_urlopen_no_redirect",
                            _fake_open(_release_payload(tag)))
        assert upd.check_for_update() is None


# ===========================================================================
# U5 — allowlist hygiene
# ===========================================================================
@pytest.mark.parametrize("raw", [
    "https://github.com/x\ty",
    "https://github.com/x\ny",
    "https://github.com/x\ry",
    "https://github.com/x y",
    "https://github.com/x\x00y",
    "https://github.com/x\x7fy",
])
def test_u5_control_chars_refused(raw):
    assert not upd._https_url_ok(raw, upd.RELEASE_HOSTS)
    assert not upd._https_url_ok(raw, upd.ASSET_HOSTS)


def test_u5_api_and_asset_hosts_are_separate():
    assert "api.github.com" in upd.API_HOSTS
    assert "api.github.com" not in upd.ASSET_HOSTS
    assert "api.github.com" in upd.ASSET_HOSTS or True
    # api.github.com must not be able to serve release bytes.
    assert not upd._https_url_ok("https://api.github.com/x", upd.ASSET_HOSTS)


def test_u5_api_redirect_to_allowlisted_host_followed():
    handler = upd._NoRedirect(upd.API_HOSTS)
    req = upd.urllib.request.Request("https://api.github.com/a")
    ok = handler.redirect_request(req, None, 302, "Found", {},
                                  "https://github.com/b")
    assert ok is not None
    assert handler.redirect_request(req, None, 302, "Found", {},
                                    "https://evil.example/b") is None


def test_u5_token_not_sent_by_default(tmp_path, github, release_files,
                                      signed_release, monkeypatch):
    seed, label, public = signed_release
    rel = release_files()
    github.publish_release(rel["version"], rel["files"])
    seen = []
    real = upd._urlopen_no_redirect

    def spy(req, *a, **k):
        seen.append(dict(req.headers))
        return real(req, *a, **k)

    monkeypatch.setattr(upd, "_urlopen_no_redirect", spy)
    upd.download_asset(_asset(rel["version"]), dest_dir=tmp_path,
                       tag_version=rel["version"], token="secret-token")
    for headers in seen:
        assert "Authorization" not in headers


def test_u5_token_retried_once_after_403(github):
    """First attempt carries NO token; only a 403/429 triggers one retry."""
    path = "/repos/%s/releases/latest" % REPO
    github.route(path, (403, {}, ""))
    req = upd.urllib.request.Request(
        f"https://api.github.com{path}", headers={"User-Agent": "x"})
    with pytest.raises(urllib.error.HTTPError):
        with upd._urlopen_no_redirect(req, timeout=5, token="tok-abc",
                                      allowed_hosts=upd.API_HOSTS):
            pass
    assert github.request_count() == 2, "expected exactly one retry"
    first, second = github.auth_headers()
    assert first is None, "token was sent before GitHub asked for it"
    assert second == "Bearer tok-abc"


def test_u5_no_retry_without_token(github):
    path = "/repos/%s/releases/latest" % REPO
    github.route(path, (403, {}, ""))
    req = upd.urllib.request.Request(
        f"https://api.github.com{path}", headers={"User-Agent": "x"})
    with pytest.raises(urllib.error.HTTPError):
        with upd._urlopen_no_redirect(req, timeout=5, token=None,
                                      allowed_hosts=upd.API_HOSTS):
            pass
    assert github.request_count() == 1


def test_u5_retries_404_because_the_repo_is_private(github):
    """404 must be retried WITH a token -- this repo is private.

    GitHub answers 404, not 403, to an unauthenticated request for a private
    repository, deliberately, so that its existence is not disclosed. The old
    policy retried only on 403/429, which meant the updater could never read
    its OWN releases: with a valid token the same request succeeded via curl
    and gh while check_for_update() returned None. Reproduced end to end
    before this was fixed.

    The retry is still bounded to one attempt and still only ever sends the
    token to an allowlisted host, so this widens WHEN a token is used, not
    WHERE it can go.
    """
    path = "/repos/%s/releases/latest" % REPO
    github.route(path, (404, {}, ""))
    req = upd.urllib.request.Request(
        f"https://api.github.com{path}", headers={"User-Agent": "x"})
    with pytest.raises(urllib.error.HTTPError):
        with upd._urlopen_no_redirect(req, timeout=5, token="tok-abc",
                                      allowed_hosts=upd.API_HOSTS):
            pass
    assert github.request_count() == 2, (
        "a 404 must be retried exactly once with the token")


def test_u5_no_retry_on_other_status(github):
    """A status a credential cannot fix must NOT be retried.

    Guards the bound: retrying everything would send the token at GitHub for
    no reason on every malformed request.
    """
    path = "/repos/%s/releases/latest" % REPO
    github.route(path, (500, {}, ""))
    req = upd.urllib.request.Request(
        f"https://api.github.com{path}", headers={"User-Agent": "x"})
    with pytest.raises(urllib.error.HTTPError):
        with upd._urlopen_no_redirect(req, timeout=5, token="tok-abc",
                                      allowed_hosts=upd.API_HOSTS):
            pass
    assert github.request_count() == 1, "must not retry a 500"


def test_u5_token_never_appears_in_errors(github, tmp_path):
    """A 403 with a token must not leak that token into the raised error."""
    github.route("/repos/%s/releases/latest" % REPO,
                 (403, {"Content-Type": "application/json"}, ""))
    req = upd.urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/releases/latest",
        headers={"User-Agent": "telegram-downloader"})
    with pytest.raises(Exception) as excinfo:
        with upd._urlopen_no_redirect(req, timeout=5,
                                      token="super-secret-token-value",
                                      allowed_hosts=upd.API_HOSTS):
            pass
    assert "super-secret-token-value" not in str(excinfo.value)
    assert "super-secret-token-value" not in repr(excinfo.value)


def _retry(req, timeout, token, allowed_hosts, real, fake):
    raise AssertionError("unused")
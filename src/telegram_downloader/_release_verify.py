"""Release-manifest signature verification (Ed25519) — the second trust anchor.

Why this module exists
----------------------
`SHA256SUMS-<plat>` used to be fetched from the SAME release, over the SAME
TLS channel, as the installer it "verified". That is not verification: whoever
can publish a release (compromised maintainer account, stolen token, forced
tag) blesses both the payload and its checksum. This module makes the
checksum manifest itself a signed document, so a release is only trusted if it
carries an Ed25519 signature from a key pinned *in this source file*.

Trust model
-----------
* The pinned keys below are literals in the source tree. They are NEVER
  fetched, imported from a network location, or read from an environment
  variable — that would just move the trust anchor to whoever controls the
  machine's environment. Adding a key is a reviewed code change.
* Verification FAILS CLOSED:
    - no signature published        -> `MissingSignatureError`
    - signature from unknown key    -> `UnknownSigningKeyError`
    - keyring empty / no key set    -> `NoPinnedSigningKeyError`
    - malformed or oversized input  -> `SignatureFormatError`
    - any Ed25519 check failure     -> `BadSignatureError`
* Only the *verify* direction is implemented. A private key has no business
  shipping in an application that only ever consumes releases.

Implementation note: pure-Python Ed25519 (RFC 8032) because the project is
strictly stdlib-only (`cryptography`/PyNaCl are not dependencies and must not
become one — an auto-installed crypto dep would be a supply-chain hole of its
own). Verification is a handful of scalar multiplications, so the cost is a few
milliseconds and it happens once per release check.

TEST VECTOR WARNING — read before trusting this code
----------------------------------------------------
`tests/test_updater_security.py` checks this implementation against signatures
produced by an INDEPENDENT implementation (OpenSSL) and against RFC 8032
vectors, so "it verifies our own signatures" is never the only evidence.
"""
from __future__ import annotations

import base64
import hashlib
import re

__all__ = [
    "PINNED_RELEASE_SIGNING_KEYS",
    "SIGNATURE_SUFFIX",
    "SIGNATURE_ENVELOPE_PREFIX",
    "MAX_SIGNATURE_BYTES",
    "MAX_MANIFEST_BYTES",
    "key_id_for",
    "keyring_state",
    "pinned_key_ids",
    "decode_public_key",
    "parse_signature_blob",
    "verify_manifest_signature",
    "MissingSignatureError",
    "UnknownSigningKeyError",
    "NoPinnedSigningKeyError",
    "SignatureFormatError",
    "BadSignatureError",
]


# --------------------------------------------------------------------------
# PINNED TRUST ANCHOR — CURRENTLY EMPTY. READ THIS.
# --------------------------------------------------------------------------
#
# >>> THIS MUST BE FILLED IN BEFORE THE IN-APP UPDATER CAN INSTALL ANYTHING. <<<
#
# With an empty keyring every manifest is refused (`NoPinnedSigningKeyError`),
# so `download_asset()` raises instead of publishing an unverified installer.
# That is deliberate: a keyring that silently accepts an unconfigured signer is
# exactly the self-assertion bug this module was written to remove.
#
# Populate it like this (one entry per signing key, old keys kept for a while
# so in-flight releases keep verifying):
#
#   1. Create the release key ONCE, offline, and store the secret key in an
#      offline/encrypted backup. Never in the repo, never in CI logs:
#          openssl genpkey -algorithm ed25519 -out release-signing.key
#   2. Print its raw 32-byte public key:
#          openssl pkey -in release-signing.key -pubout -outform DER | tail -c 32
#      and base64 that:
#          openssl pkey -in release-signing.key -pubout -outform DER \
#              | tail -c 32 | base64
#   3. Paste it here, keeping the descriptive id:
#          "ed25519:2026-01": "<base64 of the 32 raw bytes>",
#
# Format: {key_id: base64(32 raw little-endian Ed25519 public key bytes)}.
# The dict is only a lookup table — every configured key is tried and the
# matching one is reported, so there is no "claimed" id for an attacker to lie
# about. Key ids are advisory labels for humans and error messages.
#
# ---------------------------------------------------------------------------
# CI: producing the signed releases  (TODO — implement in .github/workflows/
# build.yml, the `release` job, after the checksum-verification step and
# before `softprops/action-gh-release` so the .sig files ride along)
# ---------------------------------------------------------------------------
#   # 1. Private key from a secret; NEVER from the repo. Generate once, keep an
#   #    offline backup:
#   #      openssl genpkey -algorithm ed25519 -out release-signing.key
#   #    store as the `RELEASE_SIGNING_KEY` repository secret (base64 PEM).
#   - name: Sign the checksum manifests (Ed25519, detached)
#     env:
#       RELEASE_SIGNING_KEY: ${{ secrets.RELEASE_SIGNING_KEY }}
#     run: |
#       set -euo pipefail
#       umask 077
#       printf '%s' "$RELEASE_SIGNING_KEY" | base64 -d > "$RUNNER_TEMP/key.pem"
#       KEYID="$(openssl pkey -in "$RUNNER_TEMP/key.pem" -pubout -outform DER \
#                 | tail -c 32 \
#                 | python3 -c 'import hashlib,sys; \
#                     print(hashlib.sha256(b"tgdl-release-key:"+sys.stdin.buffer.read()) \
#                     .hexdigest()[:8])')"
#       for f in SHA256SUMS-deb SHA256SUMS-apk SHA256SUMS-dmg; do
#         case "$f" in                 # NB: `[ "$f" = x ] && dir=y` under
#           SHA256SUMS-apk) dir=apk;;  #     `set -e` aborts the step when the
#           SHA256SUMS-dmg) dir=macos;; #    test is false — use case instead
#           *) dir=deb;;
#         esac
#         openssl pkeyutl -sign -inkey "$RUNNER_TEMP/key.pem" -rawin \
#           -in "$dir/$f" -out "$dir/$f.sig.raw"
#         printf 'tgdl-ed25519-v1 %s %s\n' "$KEYID" \
#           "$(base64 -w0 < "$dir/$f.sig.raw")" > "$dir/$f.sig"
#         rm -f "$dir/$f.sig.raw"
#       done
#       # Fail the release loudly if we produced no signatures at all.
#       ls -l deb/SHA256SUMS-deb.sig apk/SHA256SUMS-apk.sig macos/SHA256SUMS-dmg.sig
#       rm -f "$RUNNER_TEMP/key.pem"
#   - name: Attach to GitHub Release
#     ...
#     with:
#       files: |
#         deb/*.deb
#         deb/SHA256SUMS-deb
#         deb/SHA256SUMS-deb.sig        # <- add these three
#         apk/*.apk
#         apk/SHA256SUMS-apk
#         apk/SHA256SUMS-apk.sig
#         macos/*.dmg
#         macos/SHA256SUMS-dmg
#         macos/SHA256SUMS-dmg.sig
#
# NOTE on `gpg --detach-sign`: the brief mentioned it, and GPG is a fine
# signing tool, but its output is an OpenPGP *packet* — accepting one would
# mean shipping an OpenPGP parser (or shelling out to gpg from inside the
# verification path, which is a dependency and a subprocess attack surface).
# The one-line Ed25519 command above keeps the verifier dependency-free and
# keeps the whole trust anchor in one auditable file. If GPG is preferred,
# replace _verify_raw() with a gpg --status-fd verification and pin the key's
# fingerprint instead; do not add a permissive OpenPGP parser.
PINNED_RELEASE_SIGNING_KEYS: dict[str, str] = {}

# Asset suffix of the detached Ed25519 signature next to each manifest:
#   SHA256SUMS-deb      (signed document)
#   SHA256SUMS-deb.sig  (detached Ed25519 signature over it)
SIGNATURE_SUFFIX = ".sig"

# Envelope used by CI so an unverifiable signature can be attributed:
#     tgdl-ed25519-v1 <8-hex key id> <base64 of the 64 raw signature bytes>
# The key id is derived from the PUBLIC key (see key_id_for), never from the
# signature, and is only a lookup hint: a wrong id fails closed, it can never
# make an unverified signature pass.
SIGNATURE_ENVELOPE_PREFIX = "tgdl-ed25519-v1"
_KEY_ID_RE = re.compile(r"^[0-9a-f]{8}$")


MAX_MANIFEST_BYTES = 64 * 1024
MAX_SIGNATURE_BYTES = 1024  # raw 64B, or a small ASCII envelope


class MissingSignatureError(Exception):
    """The release published no detached signature for the manifest."""


class UnknownSigningKeyError(Exception):
    """A signature is present but matches none of the pinned public keys."""


class NoPinnedSigningKeyError(Exception):
    """No release signing key is pinned, so nothing can be trusted."""


class SignatureFormatError(Exception):
    """The signature blob is not a well-formed 64-byte Ed25519 signature."""


class BadSignatureError(Exception):
    """The signature does not verify against the manifest bytes."""


# --------------------------------------------------------------------------
# Pure-Python Ed25519 (RFC 8032) — verify only.
# --------------------------------------------------------------------------
_P = 2 ** 255 - 19
_Q = 2 ** 252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _sha512(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


def _inv(x: int) -> int:
    return pow(x, _P - 2, _P)


def _x_recover(y: int) -> int:
    xx = (y * y - 1) * _inv(_D * y * y + 1) % _P
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = x * _I % _P
    if x % 2 != 0:
        x = _P - x
    return x


_BY = 4 * _inv(5) % _P
_BX = _x_recover(_BY)
_B = (_BX % _P, _BY % _P, 1, _BX * _BY % _P)
_IDENT = (0, 1, 1, 0)
_SIGNATURE_LEN = 64
_PUBLIC_KEY_LEN = 32


def _point_add(p: tuple[int, int, int, int],
               q: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Extended-homogeneous coordinates (X:Y:Z:T) addition on edwards25519."""
    a = (p[1] - p[0]) * (q[1] - q[0]) % _P
    b = (p[1] + p[0]) * (q[1] + q[0]) % _P
    c = 2 * p[3] * q[3] * _D % _P
    dd = 2 * p[2] * q[2] % _P
    e, f, g, h = b - a, dd - c, dd + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _scalar_mult(point: tuple[int, int, int, int],
                 scalar: int) -> tuple[int, int, int, int]:
    result = _IDENT
    while scalar > 0:
        if scalar & 1:
            result = _point_add(result, point)
        point = _point_add(point, point)
        scalar >>= 1
    return result


def _point_equal(p: tuple[int, int, int, int],
                 q: tuple[int, int, int, int]) -> bool:
    if (p[0] * q[2] - q[0] * p[2]) % _P != 0:
        return False
    if (p[1] * q[2] - q[1] * p[2]) % _P != 0:
        return False
    return True


def _point_decompress(data: bytes) -> tuple[int, int, int, int] | None:
    """Decode a compressed point. None == not a canonical curve point."""
    if len(data) != 32:
        return None
    encoded = int.from_bytes(data, "little")
    sign = encoded >> 255
    y = encoded & ((1 << 255) - 1)
    if y >= _P:  # non-canonical y encoding (allows signature malleability)
        return None
    x = _x_recover(y)
    if x & 1 != sign:
        x = _P - x
    point = (x, y, 1, x * y % _P)
    if not _is_on_curve(point):
        return None
    return point


def _is_on_curve(point: tuple[int, int, int, int]) -> bool:
    x, y, z, t = point
    return (
        (-x * x + y * y - z * z - _D * t * t) % _P == 0
        and (x * y % _P) == (z * t % _P)
    )


def _verify_raw(public_key: bytes, signature: bytes, message: bytes) -> bool:
    """RFC 8032 §5.1.7 verification. Returns False for every malformed input."""
    if len(public_key) != _PUBLIC_KEY_LEN or len(signature) != _SIGNATURE_LEN:
        return False
    a_point = _point_decompress(public_key)
    if a_point is None:
        return False
    r_bytes = signature[:32]
    s = int.from_bytes(signature[32:], "little")
    if s >= _Q:  # non-canonical / malleable S: reject outright
        return False
    r_point = _point_decompress(r_bytes)
    if r_point is None:
        return False
    k = int.from_bytes(_sha512(r_bytes + public_key + message), "little") % _Q
    # [S]B == R + [k]A
    left = _scalar_mult(_B, s)
    right = _point_add(r_point, _scalar_mult(a_point, k))
    return _point_equal(left, right)


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def key_id_for(public_key: bytes) -> str:
    """Short stable label for a public key (first 4 bytes of SHA-256)."""
    return hashlib.sha256(b"tgdl-release-key:" + public_key).hexdigest()[:8]


def pinned_key_ids() -> list[str]:
    """Configured key ids, without exposing the keys themselves."""
    out = []
    for label, encoded in PINNED_RELEASE_SIGNING_KEYS.items():
        out.append(f"{label} (id {key_id_for(decode_public_key(encoded))})"
                   if _is_valid_b64_key(encoded) else f"{label} (INVALID KEY)")
    return out


def _is_valid_b64_key(encoded: str) -> bool:
    try:
        return len(decode_public_key(encoded)) == _PUBLIC_KEY_LEN
    except Exception:
        return False


def decode_public_key(encoded: str) -> bytes:
    """base64 -> 32 raw bytes, with strict validation."""
    if not isinstance(encoded, str) or not encoded:
        raise SignatureFormatError("Pinned key is not a string.")
    try:
        raw = base64.b64decode(encoded.encode("ascii"), validate=True)
    except Exception as exc:
        raise SignatureFormatError("Pinned key is not valid base64.") from exc
    if len(raw) != _PUBLIC_KEY_LEN:
        raise SignatureFormatError(
            f"Pinned key must be 32 raw bytes, got {len(raw)}.")
    return raw


def keyring_state() -> str:
    """Human-readable state of the trust anchor, for error messages/logs."""
    if not PINNED_RELEASE_SIGNING_KEYS:
        return ("NO release signing key is pinned in "
                "telegram_downloader/_release_verify.py — see the TODO there. "
                "Unsigned/unverifiable releases are refused on purpose.")
    return ("Pinned release signing keys: "
            + ", ".join(pinned_key_ids()))


def parse_signature_blob(blob: bytes) -> tuple[str | None, bytes]:
    """Return (key_id_or_None, 64 raw signature bytes).

    Two accepted forms, both strict:
      * the envelope CI publishes:
            tgdl-ed25519-v1 <8-hex key id> <base64 signature>
      * the bare 64 raw signature bytes (no key id).

    No guessing at other armoured/hex/pem forms: a permissive parser here is
    exactly where a signature we did not intend to read could sneak in.
    """
    if not isinstance(blob, (bytes, bytearray)):
        raise SignatureFormatError("Signature is not bytes.")
    blob = bytes(blob)
    if len(blob) > MAX_SIGNATURE_BYTES:
        raise SignatureFormatError("Signature blob too large.")
    if len(blob) == _SIGNATURE_LEN:
        return None, blob
    try:
        text = blob.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise SignatureFormatError(
            f"Expected a raw {_SIGNATURE_LEN}-byte Ed25519 signature or the "
            f"{SIGNATURE_ENVELOPE_PREFIX} envelope.") from exc
    parts = text.split()
    if len(parts) != 3 or parts[0] != SIGNATURE_ENVELOPE_PREFIX:
        raise SignatureFormatError(
            f"Expected a raw {_SIGNATURE_LEN}-byte Ed25519 signature or a "
            f"'{SIGNATURE_ENVELOPE_PREFIX} <key-id> <base64>' envelope.")
    if not _KEY_ID_RE.match(parts[1]):
        raise SignatureFormatError(f"Bad signature key id: {parts[1]!r}")
    try:
        raw = base64.b64decode(parts[2].encode("ascii"), validate=True)
    except Exception as exc:
        raise SignatureFormatError("Signature is not valid base64.") from exc
    if len(raw) != _SIGNATURE_LEN:
        raise SignatureFormatError(
            f"Expected a {_SIGNATURE_LEN}-byte signature, got {len(raw)}.")
    return parts[1], raw


def verify_manifest_signature(manifest: bytes, signature: bytes | None,
                              keys: dict[str, str] | None = None) -> str:
    """Verify *manifest* against a detached Ed25519 *signature*.

    Returns the label of the key that verified. Raises (never returns a
    "close enough") on every failure path:

        MissingSignatureError    signature is None/absent
        NoPinnedSigningKeyError  PINNED_RELEASE_SIGNING_KEYS is empty
        SignatureFormatError     signature blob is malformed
        UnknownSigningKeyError   signed by a key this build does not pin
        BadSignatureError        signature does not verify for this manifest
    """
    if signature is None:
        raise MissingSignatureError(
            "This release is not signed (no detached Ed25519 signature "
            f"published). {keyring_state()} Signed releases only are "
            "accepted; pass an explicit opt-in to allow unsigned releases.")
    if not isinstance(manifest, (bytes, bytearray)):
        raise SignatureFormatError("Manifest is not bytes.")
    manifest = bytes(manifest)
    if len(manifest) > MAX_MANIFEST_BYTES:
        raise SignatureFormatError("Manifest too large to verify.")

    keyring = PINNED_RELEASE_SIGNING_KEYS if keys is None else keys
    if not keyring:
        raise NoPinnedSigningKeyError(
            "No release signing key is pinned in this build, so the release "
            "cannot be trusted. This is a build/config problem, not a "
            "reason to fall back to an unsigned installer. See "
            "telegram_downloader/_release_verify.py for the TODO.")

    claimed_id, sig = parse_signature_blob(signature)
    by_id = {key_id_for(decode_public_key(encoded)): label
             for label, encoded in keyring.items()}
    if claimed_id is not None and claimed_id not in by_id:
        raise UnknownSigningKeyError(
            f"Release is signed by key id {claimed_id}, which is NOT pinned "
            f"in this build (pinned: {', '.join(sorted(by_id)) or 'none'}). "
            "Refusing: an unknown signer is not a trust anchor.")
    candidates = [by_id[claimed_id]] if claimed_id else sorted(keyring)
    for label in candidates:
        public_key = decode_public_key(keyring[label])
        if _verify_raw(public_key, sig, manifest):
            return label
    if claimed_id is not None:
        # Known key, signature simply does not verify => tampered manifest.
        raise BadSignatureError(
            "Release manifest does not match its signature (the manifest or "
            "the installer digests in it were modified); refusing.")
    if len(candidates) == 1:
        raise BadSignatureError(
            "Release manifest does not match its signature from the single "
            f"pinned key {candidates[0]!r}; refusing.")
    raise UnknownSigningKeyError(
        "Release is signed by a key that is NOT pinned in this build "
        f"(tried {len(candidates)} key(s): {', '.join(candidates)}). "
        "Refusing: an unknown signer is not a trust anchor.")

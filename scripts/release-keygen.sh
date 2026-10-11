#!/usr/bin/env bash
# Generate the Ed25519 release-signing keypair and prove it works end to end.
#
# WHY THIS IS A SCRIPT AND NOT SOMETHING THE APP DOES AT RUNTIME
# -------------------------------------------------------------
# The pinned public key IS the trust anchor: it is what makes "this release
# came from us" mean anything. If the application generated its own key on
# first run and trusted it, anyone who could replace the binary could replace
# the key with it, and the whole scheme collapses back into self-assertion --
# exactly the bug _release_verify.py exists to remove. The key must therefore
# be created ONCE, offline, by a human, and only its PUBLIC half travels.
#
# So automate the mechanics, never the trust decision. This script does the
# error-prone parts (openssl incantations, key-id derivation that must match
# key_id_for() byte for byte, and a real round-trip proof) and then tells you
# exactly what to paste where.
#
# Usage:  scripts/release-keygen.sh [output-dir]
# Never prints the private key. Writes it to <dir>/release-signing.key (0600).

set -euo pipefail

cd "$(dirname "$0")/.."
OUT_DIR="${1:-./release-keys}"
mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"
KEY="$OUT_DIR/release-signing.key"

if [ -e "$KEY" ]; then
  echo "error: $KEY already exists." >&2
  echo "Never regenerate a release key in place — in-flight releases stop" >&2
  echo "verifying and every user must reinstall. Create a NEW key in a new" >&2
  echo "directory and keep the old public key pinned until its releases age out." >&2
  exit 1
fi

if ! command -v openssl >/dev/null 2>&1; then
  echo "error: openssl not found. Install it and re-run." >&2
  exit 1
fi

# 077 before the key exists on disk, so it is never briefly world-readable.
umask 077
echo "==> generating Ed25519 keypair (private key stays in $KEY)"
openssl genpkey -algorithm ed25519 -out "$KEY"
chmod 600 "$KEY"

# An SPKI DER blob for Ed25519 is exactly 44 bytes: a 12-byte header followed
# by the 32 raw public key bytes.
PUB_B64="$(openssl pkey -in "$KEY" -pubout -outform DER | tail -c 32 | base64 -w0)"
# MUST match _release_verify.key_id_for():
#   sha256(b"tgdl-release-key:" + raw32).hexdigest()[:8]
KEY_ID="$(printf '%s' "$PUB_B64" | base64 -d | python3 -c \
  'import hashlib,sys; print(hashlib.sha256(b"tgdl-release-key:"+sys.stdin.buffer.read()).hexdigest()[:8])')"

echo "==> proving the round-trip against the app's own verifier"
# Sign a throwaway manifest with the exact CI command, then verify it with the
# real _release_verify code. A key that cannot pass this test must never be
# pinned -- otherwise the app refuses every release at install time.
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
printf 'probe  telegram-downloader_0.0.0_amd64.deb\n' > "$TMP/SHA256SUMS-deb"
openssl pkeyutl -sign -inkey "$KEY" -rawin \
  -in "$TMP/SHA256SUMS-deb" -out "$TMP/sig.raw"
printf 'tgdl-ed25519-v1 %s %s\n' "$KEY_ID" "$(base64 -w0 < "$TMP/sig.raw")" \
  > "$TMP/SHA256SUMS-deb.sig"

PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  if [ -x .venv/bin/python ]; then PYTHON=.venv/bin/python; else PYTHON=python3; fi
fi

if ! "$PYTHON" - "$TMP" "$PUB_B64" <<'PYEOF'
import sys, pathlib
sys.path.insert(0, "src")
from telegram_downloader import _release_verify as rv
tmp = pathlib.Path(sys.argv[1]); pub = sys.argv[2]
manifest = (tmp / "SHA256SUMS-deb").read_bytes()
sig = (tmp / "SHA256SUMS-deb.sig").read_bytes()
who = rv.verify_manifest_signature(manifest, sig, keys={"probe": pub})
assert who == "probe", who
# Negative control: a tampered manifest must NOT verify.
try:
    rv.verify_manifest_signature(manifest + b"x", sig, keys={"probe": pub})
except Exception:
    print("    round-trip OK; tampered manifest correctly refused")
else:
    raise SystemExit("FATAL: tampered manifest verified")
PYEOF
then
  echo "error: the generated key does not verify against _release_verify." >&2
  echo "Do NOT pin it. Report this as a bug." >&2
  exit 1
fi

echo
echo "================================================================"
echo " Public key id:  $KEY_ID"
echo " Private key:    $KEY  (0600, never committed, never printed)"
echo " Back it up offline NOW. There is no recovery if it is lost."
echo "================================================================"
echo
echo "STEP 1 — pin the PUBLIC key in src/telegram_downloader/_release_verify.py:"
echo
echo '    PINNED_RELEASE_SIGNING_KEYS: dict[str, str] = {'
echo '        "ed25519:2026-10": "'"$PUB_B64"'",'
echo '    }'
echo
echo "STEP 2 — store the PRIVATE key as the repo secret RELEASE_SIGNING_KEY:"
echo
echo "    base64 -w0 '$KEY'"
echo
echo "  ...or let gh do it (the secret is never printed to your terminal):"
echo
echo "    base64 -w0 '$KEY' | gh secret set RELEASE_SIGNING_KEY"
echo
echo "STEP 3 — confirm the app agrees before tagging a release:"
echo
echo "    $PYTHON -c 'import sys; sys.path.insert(0,\"src\"); from telegram_downloader import _release_verify as rv; s=rv.validate_pinned_keys(); print(s.summary); raise SystemExit(0 if s.ok else 1)'"
echo
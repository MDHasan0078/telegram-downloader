#!/usr/bin/env bash
# Fail loudly if release-keys/ holds a key that is NOT the one pinned in
# _release_verify.py (or vice versa).
#
# Why this exists: the signing key and the public key pinned in the app are
# two halves of one trust anchor, kept in two different places by necessity
# (one in the repo, one in GitHub's secret store). Nothing stops them
# drifting except this check, and a drift is silent and total -- CI would
# sign with a key the app refuses, and every install would fail closed with
# a signature error that looks like tampering.
#
# Run from the repo root. Exits non-zero on mismatch.
set -euo pipefail
cd "$(dirname "$0")/.."

KEYFILE="release-keys/release-signing.key"
if [ ! -f "$KEYFILE" ]; then
  echo "OK (no local key): $KEYFILE not present -- nothing to compare."
  exit 0
fi

ON_DISK="$(openssl pkey -in "$KEYFILE" -pubout -outform DER | tail -c 32 | base64 -w0)"
PINNED="$(python3 - <<'PY'
import re, pathlib
src = pathlib.Path("src/telegram_downloader/_release_verify.py").read_text()
m = re.search(r"PINNED_RELEASE_SIGNING_KEYS[^=]*=\s*\{(.*?)\}", src, re.S)
if not m:
    print(""); raise SystemExit
keys = re.findall(r'"[0-9A-Za-z:_\-]+"\s*:\s*"([A-Za-z0-9+/=]+)"', m.group(1))
print(keys[0] if keys else "")
PY
)"

if [ -z "$PINNED" ]; then
  echo "MISMATCH: no public key is pinned in _release_verify.py." >&2
  echo "  Run: scripts/release-keygen.sh, then pin the printed key." >&2
  exit 1
fi

if [ "$ON_DISK" = "$PINNED" ]; then
  echo "OK: local key matches the pinned public key."
  exit 0
fi

echo "MISMATCH between the local signing key and the pinned public key:" >&2
echo "  on disk : $ON_DISK" >&2
echo "  pinned  : $PINNED" >&2
echo >&2
echo "Every install would fail closed with a signature error. Either pin the" >&2
echo "on-disk key, or set RELEASE_SIGNING_KEY from this file:" >&2
echo "  base64 -w0 $KEYFILE | gh secret set RELEASE_SIGNING_KEY" >&2
exit 1
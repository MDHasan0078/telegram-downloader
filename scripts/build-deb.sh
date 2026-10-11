#!/bin/bash
# Build a .deb for Debian/Ubuntu (Linux target).
# Produces dist/telegram-downloader_<ver>_amd64.deb
# Requires: python3 (>=3.10), pip, dpkg-deb, git. Run on Linux.
#
# Security properties this script is responsible for:
#   * hermetic end-user install -- every runtime wheel is vendored into the
#     .deb and installed with --no-index --require-hashes --only-binary, so
#     `run.sh` can never pull a floating distribution off PyPI as the user;
#   * no stale build/ -- setuptools stages src/ into build/lib/ with
#     update=1, which SKIPS a build/lib file newer than its src/ twin, so
#     build/, dist/ and *.egg-info are deleted before anything is packaged and
#     the built wheel is diffed against src/ afterwards;
#   * reproducible -- SOURCE_DATE_EPOCH pins every archive member's timestamp.
set -euo pipefail
cd "$(dirname "$0")/.."

# ---------------------------------------------------------------- clean slate
# MUST come first, before anything can create build/ or dist/. `build/` is
# gitignored, so on a developer machine (or a cached CI runner) it can still
# hold an older - or hostile - snapshot that setuptools will happily stage into
# the wheel. Deleting it here, unconditionally, is the fix; the wheel<->src
# diff below is the belt to that braces.
echo "==> cleaning build/ dist/ *.egg-info"
rm -rf build dist
rm -rf ./*.egg-info src/*.egg-info
mkdir -p dist

# ------------------------------------------------------- reproducible builds
# setuptools stamps each zip member with wall-clock time, so two builds of one
# commit otherwise disagree bit-for-bit and no published checksum means
# anything. Bind it to the commit timestamp instead.
if [ -z "${SOURCE_DATE_EPOCH:-}" ]; then
  SOURCE_DATE_EPOCH="$(git log -1 --pretty=%ct 2>/dev/null || true)"
fi
if ! printf '%s' "$SOURCE_DATE_EPOCH" | grep -qE '^[0-9]+$'; then
  echo "build-deb.sh: cannot determine SOURCE_DATE_EPOCH (no git history and" >&2
  echo "  SOURCE_DATE_EPOCH unset). Refusing to emit a non-reproducible build." >&2
  exit 1
fi
export SOURCE_DATE_EPOCH
export PYTHONHASHSEED=0
echo "==> SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH"

# tomllib needs py3.11+; parse with regex so py3.9/3.10 don't silently
# fall back to a wrong 0.1.0 version (fail loudly instead).
# NOTE: print() MUST come before sys.exit() — sys.exit raises SystemExit
# immediately, so any print after it is dead code (VER would be empty).
VER="$(python3 -c '
import re, sys
with open("pyproject.toml") as f:
    toml = f.read()
m = re.search(r"^version\s*=\s*\"([^\"]+)\"", toml, re.M)
if not m:
    sys.stderr.write("build-deb.sh: cannot find version in pyproject.toml\n")
    sys.exit(1)
print(m.group(1))
')"
# Reject obviously-bad versions (empty / unparsable) with a loud build error.
if ! printf '%s' "$VER" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo "build-deb.sh: invalid version in pyproject.toml: '$VER'" >&2
  exit 1
fi

# ------------------------------------------------------- vendored wheel set
# The .deb ships wheels/ + requirements-runtime.lock. run.sh installs from
# those alone with --no-index, so this directory IS the end user's trust root.
# A partial or unmanifested wheel set must fail the BUILD, never surface as a
# network fetch on the user's machine at first launch.
LOCK="requirements-runtime.lock"
WHEELDIR="wheels"
for f in "$LOCK" requirements-build.lock; do
  [ -f "$f" ] || {
    echo "build-deb.sh: $f is missing. It pins+hashes the runtime closure /" >&2
    echo "  build backend; without it the .deb would resolve floating versions" >&2
    echo "  from PyPI as the end user. See each file's header to regenerate." >&2
    exit 1
  }
done
[ -f "$WHEELDIR/SHA256SUMS" ] || {
  echo "build-deb.sh: $WHEELDIR/SHA256SUMS is missing; refusing to ship an" >&2
  echo "  unverified wheel set." >&2
  exit 1
}
echo "==> verifying vendored wheels against $WHEELDIR/SHA256SUMS"
( cd "$WHEELDIR" && sha256sum -c --quiet SHA256SUMS ) || {
  echo "build-deb.sh: vendored wheel checksum mismatch in $WHEELDIR/" >&2
  exit 1
}
# Two-way completeness: a wheel nobody pinned, and a pin with no wheel.
UNMANIFESTED="$( cd "$WHEELDIR" && comm -13 \
  <(awk '{print $2}' SHA256SUMS | sort) <(ls -1 ./*.whl 2>/dev/null | sed 's#^\./##' | sort) )"
[ -z "$UNMANIFESTED" ] || {
  echo "build-deb.sh: wheels present but absent from SHA256SUMS:" >&2
  printf '  %s\n' $UNMANIFESTED >&2
  exit 1
}
MISSING_WHEEL="$( cd "$WHEELDIR" && comm -23 \
  <(awk '{print $2}' SHA256SUMS | sort) <(ls -1 ./*.whl 2>/dev/null | sed 's#^\./##' | sort) )"
[ -z "$MISSING_WHEEL" ] || {
  echo "build-deb.sh: SHA256SUMS references files that are not present:" >&2
  printf '  %s\n' $MISSING_WHEEL >&2
  exit 1
}
# Every requirement in the lock must be satisfiable from the vendored wheels
# alone, otherwise --only-binary --no-index fails in the user's terminal.
python3 - "$WHEELDIR" "$LOCK" <<'PY' || exit 1
import re, sys, pathlib
wheel_dir, lock = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
have = sorted(p.name[:-4].split("-")[0].replace("_", "-").lower()
              for p in wheel_dir.glob("*.whl"))
have_set = set(have)
missing = []
name = None
for raw in lock.read_text().splitlines():
    line = raw.split("#", 1)[0].strip().rstrip("\\").strip()
    if not line or line.startswith("--hash"):
        continue
    m = re.match(r"^([A-Za-z0-9._-]+)==", line)
    if m:
        name = m.group(1).replace("_", "-").replace(".", "-").lower()
        if name not in have_set:
            missing.append(line)
if missing:
    sys.stderr.write(
        "build-deb.sh: $LOCK pins requirements with no vendored wheel in "
        f"{wheel_dir}/ — the .deb install would fail on the user's machine:\n")
    for line in missing:
        sys.stderr.write(f"  - {line}\n")
    sys.stderr.write("  Refusing to build an incomplete wheel set.\n")
    sys.exit(1)
print(f"==> {len(have_set)} vendored wheels cover every pin in {lock}")
PY

PKG="dist/debroot"
rm -rf "$PKG"
mkdir -p "$PKG/DEBIAN" "$PKG/opt/telegram-downloader" "$PKG/usr/bin" "$PKG/usr/share/applications" "$PKG/usr/share/icons/hicolor/scalable/apps"

# python3 version bounds mirror the msgpack cp310..cp314 wheels vendored in
# wheels/: the venv is built from the *system* interpreter, so the user's
# python3 minor version decides which wheel --only-binary can pick.
cat > "$PKG/DEBIAN/control" <<EOF
Package: telegram-downloader
Version: $VER
Section: video
Priority: optional
Architecture: amd64
Depends: python3 (>= 3.10), python3 (<< 3.15), python3-venv, python3-pip, ffmpeg
Maintainer: tgdownloader <noreply@example.com>
Description: Telegram media downloader (GUI + CLI)
 Persistent login, resumable downloads, queue, MP4 conversion.
EOF

cp -r src pyproject.toml README.md "$PKG/opt/telegram-downloader/"
cp -r "$WHEELDIR" "$LOCK" "$PKG/opt/telegram-downloader/"
rm -f "$PKG/opt/telegram-downloader/$WHEELDIR/SHA256SUMS"

# Prebuild a wheel: installing from source at runtime fails for non-root
# users (setuptools cannot write egg-info into root-owned /opt).
#   --no-build-isolation  -> do not download+execute a build backend (which
#                            would run with a live token/network in CI, and as
#                            the end user at install time).
#   --no-cache-dir        -> keep pip's on-disk cache out of the image.
# --no-build-isolation means the backend is whatever is already importable, so
# that backend has to exist and has to be the pinned one: Python >= 3.12's
# ensurepip no longer installs setuptools at all, and letting pip fetch
# `setuptools>=68` at build time is the exact unpinned-execution path above.
#
# This install is UNCONDITIONAL and its failure is fatal. It used to be guarded
# by `if ! python3 -c 'import setuptools'`, which is backwards for the thing it
# was protecting: the runner image always has an importable setuptools, so the
# guard was false exactly when it mattered and the .deb was built with whatever
# version ubuntu-latest happened to ship that week. "Some setuptools exists" is
# not the requirement; "this setuptools is the one in requirements-build.lock"
# is, so check that instead of checking for existence.
#
# `python3 -m pip` rather than bare `pip`: it is the one guaranteed to belong to
# the same interpreter that will import the backend, which is the whole basis of
# the --no-build-isolation argument above. A bare `pip` can be a different
# interpreter's (or a venv's), in which case the pin lands somewhere the build
# never looks.
echo "==> installing pinned build backend"
if ! python3 -m pip install --require-hashes --only-binary :all: --no-cache-dir \
     -r requirements-build.lock; then
  echo "build-deb.sh: could not install the pinned build backend from" >&2
  echo "  requirements-build.lock. Refusing to fall back to whatever setuptools" >&2
  echo "  this machine happens to have: --no-build-isolation would then build" >&2
  echo "  the shipped .deb with an unpinned, unverified backend." >&2
  exit 1
fi

# Prove the pin actually took, rather than trusting that the install above
# landed where the build reads from. Both the version and the import path have
# to match requirements-build.lock, otherwise the wheel below is still built by
# whatever `import setuptools` finds first.
python3 - <<'PY' || exit 1
import re, sys, pathlib, subprocess
lock = pathlib.Path("requirements-build.lock").read_text()
m = re.search(r"^setuptools==([^\s\\]+)", lock, re.M)
if not m:
    sys.stderr.write("build-deb.sh: no setuptools== pin in requirements-build.lock\n")
    sys.exit(1)
pinned = m.group(1)
actual = subprocess.run(
    [sys.executable, "-c",
     "import setuptools;print(setuptools.__version__, setuptools.__file__)"],
    capture_output=True, text=True, check=True).stdout.split()
if actual[0] != pinned:
    sys.stderr.write(
        "build-deb.sh: the build backend in use is setuptools "
        f"{actual[0]} ({actual[1]}), but requirements-build.lock pins "
        f"{pinned}.\n  The shipped .deb would be built by an unpinned backend.\n")
    sys.exit(1)
print(f"==> build backend is the pinned setuptools {pinned} ({actual[1]})")
PY

echo "==> building project wheel (no build isolation)"
python3 -m pip wheel --no-deps --no-build-isolation --no-cache-dir \
  -w "$PKG/opt/telegram-downloader/$WHEELDIR" . 2>&1 | tail -2

PROJ_WHEELS=( "$PKG/opt/telegram-downloader/$WHEELDIR"/telegram_downloader-*.whl )
if [ "${#PROJ_WHEELS[@]}" -ne 1 ]; then
  echo "build-deb.sh: expected exactly 1 project wheel, found ${#PROJ_WHEELS[@]}" >&2
  exit 1
fi
PROJ_WHEEL="${PROJ_WHEELS[0]}"

# Second gate on C2: whatever `build/` contained, the artifact that is about to
# be installed must equal src/ byte for byte.
echo "==> verifying project wheel matches src/"
python3 scripts/verify_wheel_matches_src.py "$PROJ_WHEEL" --src src || exit 1

# Pin + hash the project wheel itself. pip's --require-hashes mode demands a
# hash for the project wheel too, and this is what makes the install refuse to
# run if the wheel in /opt was swapped after the build.
PROJ_HASH="$(sha256sum "$PROJ_WHEEL" | cut -d' ' -f1)"
# Read Name/Version out of the wheel's own METADATA rather than parsing the
# filename: wheel names carry an optional build tag, so positional shell
# splitting gets it wrong often enough to ship a broken pin.
read -r PROJ_NAME PROJ_VER < <(python3 - "$PROJ_WHEEL" <<'PY'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as zf:
    meta = next(n for n in zf.namelist()
                if n.endswith(".dist-info/METADATA") and n.count("/") == 1)
    fields = {}
    for line in zf.read(meta).decode("utf-8", "replace").splitlines():
        if not line.strip():
            break
        k, _, v = line.partition(":")
        fields.setdefault(k.strip().lower(), v.strip())
print(fields["name"], fields["version"])
PY
)
{
  echo "# Generated by scripts/build-deb.sh at build time. Pins the artifact"
  echo "# built in this job so run.sh's --require-hashes install covers it too."
  echo "$PROJ_NAME==$PROJ_VER \\"
  echo "    --hash=sha256:$PROJ_HASH"
} > "$PKG/opt/telegram-downloader/requirements-project.lock"

cat > "$PKG/opt/telegram-downloader/run.sh" <<'EOF'
#!/bin/bash
set -euo pipefail
shopt -s nullglob
APP=/opt/telegram-downloader
SYS_VENV="$APP/.venv"
# ${HOME:-} + mkdir -p: set -u must not explode when HOME is unset, and
# the per-user venv parent may not exist yet.
USER_VENV="${XDG_DATA_HOME:-${HOME:-/root}/.local/share}/telegram-downloader/.venv"
# Prefer the system venv only if we can write to it (root/admin, or a
# user-writable install); otherwise use a per-user venv since regular
# users cannot write to /opt (a stale root-owned venv must NOT be reused).
if [ -w "$SYS_VENV" ] || { [ ! -e "$SYS_VENV" ] && [ -w "$APP" ]; }; then
  VENV="$SYS_VENV"
else
  VENV="$USER_VENV"
fi
mkdir -p "$(dirname "$VENV")"
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"

# ---------------------------------------------------------------------------
# HERMETIC INSTALL. Everything below is deliberately incapable of reaching the
# network:
#   --no-index           no PyPI (or any index) lookup, ever;
#   --find-links         the wheel set baked into /opt by the .deb;
#   --require-hashes    every artifact must match a sha256 recorded at build
#                       time -- a tampered or substituted wheel aborts the
#                       install instead of executing;
#   --only-binary :all:  refuse to build C from an sdist as this (usually
#                       unprivileged) user, e.g. on musl/Termux;
#   --no-build-isolation never fetch+execute a build backend.
# There is no "on failure, fall back to a plain pip install" path any more:
# degrading to one is exactly how an unpinned PyPI install sneaks back in, so a
# failed hermetic install is fatal and says so.
# ---------------------------------------------------------------------------
export PIP_DISABLE_PIP_VERSION_CHECK=1
WHEELS=( "$APP"/wheels/telegram_downloader-*.whl )
if [ "${#WHEELS[@]}" -ne 1 ]; then
  echo "tg-dl: expected exactly 1 project wheel in $APP/wheels, found ${#WHEELS[@]}" >&2
  exit 1
fi
WHEEL="${WHEELS[0]}"
# Key the cache on the wheel's hash, not its version: same version + different
# bytes (rebuild, tampering) must force a reinstall.
MARKER="$VENV/.tgdl-wheel"
WHEEL_ID="$(sha256sum "$WHEEL" | cut -d' ' -f1)"
# Install only when the venv is new or the wheel changed — not on every launch
# (this is fast, but it is also the only step that writes to disk).
if [ ! -f "$MARKER" ] || [ "$(cat "$MARKER" 2>/dev/null)" != "$WHEEL_ID" ]; then
  if ! "$VENV/bin/pip" install \
        --no-index \
        --find-links "$APP/wheels" \
        --require-hashes \
        --only-binary :all: \
        --no-build-isolation \
        --no-cache-dir \
        -r "$APP/requirements-runtime.lock" \
        -r "$APP/requirements-project.lock"; then
    echo "tg-dl: ERROR: offline install from $APP/wheels failed." >&2
    echo "tg-dl: This build is not self-contained; report it. Nothing was" >&2
    echo "tg-dl: downloaded from PyPI, so retrying without fixing the .deb" >&2
    echo "tg-dl: cannot help." >&2
    exit 1
  fi
  echo "$WHEEL_ID" > "$MARKER"
fi
exec "$VENV/bin/tg-dl" "$@"
EOF
chmod +x "$PKG/opt/telegram-downloader/run.sh"
ln -sf /opt/telegram-downloader/run.sh "$PKG/usr/bin/tg-dl"
cat > "$PKG/usr/share/applications/telegram-downloader.desktop" <<'EOF'
[Desktop Entry]
Name=Telegram Downloader
Exec=tg-dl gui
Icon=telegram-downloader
Terminal=false
Type=Application
Categories=AudioVideo;Network;
EOF
[ -f assets/icon.svg ] && cp assets/icon.svg "$PKG/usr/share/icons/hicolor/scalable/apps/telegram-downloader.svg" || true
chmod 0755 "$PKG/DEBIAN"
DEB="dist/telegram-downloader_${VER}_amd64.deb"
dpkg-deb --build "$PKG" "$DEB"
echo "Built $DEB"
echo "Install: sudo dpkg -i dist/*.deb && tg-dl gui"

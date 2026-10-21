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

# --- msgpack must stay the pure-python wheel ------------------------------
# requirements-runtime.lock pins msgpack-1.2.3-py3-none-any.whl, whose hash is
# NOT the one PyPI publishes (PyPI has no such artifact; see the lock header).
# That makes it the one wheel here whose provenance is enforced by this script
# rather than by "the hash matches the upstream file", so the shape has to be
# re-asserted on every build: exactly one msgpack wheel, pure-python, carrying
# the fallback implementation and no compiled extension. Otherwise the next
# `pip download msgpack` quietly puts five 460 KB binary wheels back into the
# package and costs 2.3 MB, with nothing failing.
python3 - "$WHEELDIR" <<'PY' || exit 1
import hashlib, base64, pathlib, sys, zipfile

wheel_dir = pathlib.Path(sys.argv[1])
wheels = sorted(wheel_dir.glob("msgpack-*.whl"))


def bail(msg):
    sys.stderr.write(f"build-deb.sh: {msg}\n")
    sys.exit(1)


if len(wheels) != 1:
    bail(f"{wheel_dir} must hold exactly one msgpack wheel, found {len(wheels)}:\n"
         + "".join(f"    {w.name}\n" for w in wheels)
         + "  Either msgpack came back as per-ABI binary wheels (2.3 MB), or it\n"
         "  went missing. Re-vendor with scripts/vendor_msgpack_purepython.py;\n"
         "  the rationale is in requirements-runtime.lock.")

with zipfile.ZipFile(wheels[0]) as zf:
    names = zf.namelist()
    if any("_cmsgpack" in n for n in names):
        bail(f"{wheels[0].name} contains a compiled extension. The pure-python "
             "wheel exists because msgpack's fallback is byte-identical on the "
             "wire and half the size; shipping the C extension restores 2.3 MB "
             "of near-duplicate binaries for one installable copy.")
    if "msgpack/fallback.py" not in names:
        bail(f"{wheels[0].name} has no msgpack/fallback.py, so removing the C "
             "extension would be a downgrade rather than a repack.")
    tag = zf.read(next(n for n in names if n.endswith(".dist-info/WHEEL"))).decode()
    if "Tag: py3-none-any" not in tag:
        bail(f"{wheels[0].name} is not a universal wheel:\n{tag}")
    # RECORD must describe the bytes that are actually here, or the wheel is a
    # hand-edited artifact whose contents nothing downstream can account for.
    record = zf.read(next(n for n in names if n.endswith(".dist-info/RECORD"))).decode()
    listed = set()
    for line in record.splitlines():
        member, _, rest = line.partition(",")
        digest, _, size = rest.partition(",")
        listed.add(member)
        if not digest:
            continue
        data = zf.read(member)
        want = "sha256=" + base64.urlsafe_b64encode(
            hashlib.sha256(data).digest()).rstrip(b"=").decode()
        if digest != want or int(size) != len(data):
            bail(f"{wheels[0].name}: RECORD disagrees with the bytes of {member}")
    if listed != set(names):
        bail(f"{wheels[0].name}: RECORD and contents disagree on "
             f"{sorted(listed ^ set(names))}")

print(f"==> msgpack is the pure-python wheel ({wheels[0].name})")
PY

# --- closure walk across the supported interpreter range ------------------
# The pins above are read by eye, at 3.12, where every `python_version < "3.11"`
# marker is False. That is how exceptiongroup went missing from wheels/ and the
# .deb installed fine and then died on the user's first launch on 3.10. So: for
# each interpreter this package claims to support, resolve the closure from the
# vendored wheels' own Requires-Dist with THAT interpreter's markers, and fail
# the build on anything unresolvable.
#
# `pip install --python-version` is not a substitute — it re-tags wheels but
# still evaluates requirement markers against the interpreter RUNNING pip, so
# it resolves a 3.10 install with exceptiongroup deleted (verified). The marker
# evaluation below is the part that catches it.
#
# SUPPORTED_PYTHONS must stay in sync with the Depends: line below, and with
# the range the lock header documents. Narrowing one without the others is
# exactly the failure this gate exists to make impossible.
SUPPORTED_PYTHONS="3.10 3.11 3.12 3.13 3.14"
python3 - "$WHEELDIR" "$LOCK" "$SUPPORTED_PYTHONS" <<'PY' || exit 1
import pathlib, re, sys, zipfile

try:  # exact PEP 508 semantics when it is importable
    from packaging.markers import Marker
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    def requirement(text):
        """-> (canonical name, marker string or "")"""
        req = Requirement(text)
        return canonicalize_name(req.name), str(req.marker or "")

    def marker_holds(marker, version):
        return Marker(marker).evaluate({"python_version": version})

except ImportError:  # and a conservative evaluator when it is not
    def canonicalize_name(name):
        return re.sub(r"[-_.]+", "-", name).lower()

    def requirement(text):
        head, _, marker = text.partition(";")
        name = re.match(r"\s*([A-Za-z0-9._-]+)", head)
        return (canonicalize_name(name.group(1)) if name else ""), marker.strip()

    def marker_holds(marker, version):
        # The only markers in this closure are python_version comparisons.
        # Anything else (a platform condition, a boolean expression) is
        # treated as TRUE: requiring a wheel we happen to ship is harmless,
        # while treating it as FALSE can silently drop a pin, which is the
        # exact bug this walk exists to catch. Failing closed is free.
        m = re.fullmatch(
            r"""\(?\s*python_version\s*(==|!=|<=|>=|<|>)\s*["']?([\d.]+)["']?\s*\)?""",
            marker)
        if not m:
            return True
        op, target = m.group(1), [int(p) for p in m.group(2).split(".")]
        actual = [int(p) for p in version.split(".")]
        actual += [0] * (len(target) - len(actual))
        target += [0] * (len(actual) - len(target))
        return {"==": actual == target, "!=": actual != target, "<": actual < target,
                "<=": actual <= target, ">": actual > target,
                ">=": actual >= target}[op]

wheel_dir, lock = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
versions = sys.argv[3].split()

# --- what we vendored, and which interpreters each wheel can serve ----------
# A wheel filename is {name}-{version}(-{build})?-{python}-{abi}-{platform}...
# so the tag section is everything after the version, and the platform may
# carry several dot-separated alternatives.
wheels, requires = {}, {}
for path in sorted(wheel_dir.glob("*.whl")):
    parts = path.name[: -len(".whl")].split("-")
    name, version = canonicalize_name(parts[0]), parts[1]
    tags = [t for t in parts[2:] if not t[:1].isdigit()]
    wheels.setdefault(name, {})[version] = (path.name, tags)
    with zipfile.ZipFile(path) as zf:
        meta = next(n for n in zf.namelist()
                    if n.endswith(".dist-info/METADATA") and n.count("/") == 1)
        for line in zf.read(meta).decode("utf-8", "replace").splitlines():
            if line.startswith("Requires-Dist:"):
                requires.setdefault(name, []).append(line[15:].strip())


def serves(tags, version):
    """Would pip --only-binary be able to pick this wheel on `version`?

    Two shapes are vendored: universal (py3-none-any, and six's py2.py3) and
    one cpXY-cpXY-manylinux wheel per ABI. Anything else is treated as
    unservable rather than guessed at, so an unrecognised wheel shape fails the
    build loudly instead of being assumed compatible.
    """
    interpreter = "cp" + version.replace(".", "")
    if len(tags) == 3 and tags[1] == "none" and tags[2] == "any":
        return any(t.startswith("py3") for t in tags[0].split("."))
    return len(tags) >= 3 and tags[0] == interpreter and tags[1] == interpreter


# What the lock actually installs. A wheel in wheels/ that nothing pins is not
# installed, so "has a wheel" is not enough on its own -- a transitively needed
# package must be pinned too, or pip resolves it offline and fails.
pinned = set()
for raw in lock.read_text().splitlines():
    pin = re.match(r"([A-Za-z0-9._-]+)==", raw.split("#", 1)[0].strip().rstrip("\\").strip())
    if pin:
        pinned.add(canonicalize_name(pin.group(1)))


failures, sizes = [], []
for version in versions:
    # What the user actually installs on this interpreter: every requirement
    # any vendored wheel declares, minus the optional extras this package does
    # not ask for, minus the ones whose markers are false here.
    wanted = set()
    for entries in requires.values():
        for text in entries:
            if re.search(r"\bextra\s*==", text):
                continue
            dep, marker = requirement(text)
            if dep and (not marker or marker_holds(marker, version)):
                wanted.add(dep)
    sizes.append(len(wanted))
    for dep in sorted(wanted):
        offered = wheels.get(dep, {})
        if dep not in pinned:
            failures.append(
                f"python {version}: '{dep}' is required by a vendored wheel but "
                f"is not pinned in {lock.name} — pip would try to resolve it "
                f"from an index, and there is no index")
        elif not offered:
            failures.append(
                f"python {version}: '{dep}' is pinned but has no wheel in "
                f"{wheel_dir}/ — the user's first launch fails")
        elif not [fn for fn, tags in offered.values() if serves(tags, version)]:
            failures.append(
                f"python {version}: '{dep}' is vendored as "
                f"{sorted(fn for fn, _ in offered.values())}, none of which "
                f"python {version} can install")

if failures:
    for line in failures:
        sys.stderr.write(f"build-deb.sh: {line}\n")
    sys.stderr.write(
        "  The supported range is the union of what this package promises and\n"
        "  what wheels/ can serve; a marker-conditional pin that goes missing\n"
        "  fails the build here instead of at the user's first launch.\n")
    sys.exit(1)
print(f"==> closure walk over python {' '.join(versions)}: "
      f"{'–'.join(str(s) for s in sizes)} requirements resolved per interpreter")
PY

PKG="dist/debroot"
rm -rf "$PKG"
mkdir -p "$PKG/DEBIAN" "$PKG/opt/telegram-downloader" "$PKG/usr/bin" "$PKG/usr/share/applications" "$PKG/usr/share/icons/hicolor/scalable/apps"

# These bounds are the range SUPPORTED_PYTHONS above walks, and they must be
# edited together: the floor is what the closure needs (flet, msgpack and the
# project all declare Requires-Python >=3.10), the ceiling is where we stop
# testing. They are no longer tied to which msgpack ABI wheels happen to be
# vendored — every runtime wheel in wheels/ is py3-none-any now — so raising
# the ceiling is a support claim that the closure walk above has to survive, not
# a packaging detail. See requirements-runtime.lock.
cat > "$PKG/DEBIAN/control" <<EOF
Package: telegram-downloader
Version: $VER
Section: video
Priority: optional
Architecture: amd64
Depends: python3 (>= 3.10), python3 (<< 3.15), python3-venv, python3-pip, ffmpeg, python3-gi, gir1.2-gtk-4.0
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
[ -f src/telegram_downloader/assets/icon.svg ] && cp src/telegram_downloader/assets/icon.svg "$PKG/usr/share/icons/hicolor/scalable/apps/telegram-downloader.svg" || true
chmod 0755 "$PKG/DEBIAN"
DEB="dist/telegram-downloader_${VER}_amd64.deb"
dpkg-deb --build "$PKG" "$DEB"
echo "Built $DEB"
echo "Install: sudo dpkg -i dist/*.deb && tg-dl gui"

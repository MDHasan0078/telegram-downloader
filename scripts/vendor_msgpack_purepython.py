#!/usr/bin/env python3
"""Re-vendor msgpack as a single py3-none-any wheel, without the C extension.

WHY THIS EXISTS
---------------
msgpack is a C extension, so PyPI publishes one wheel PER CPython ABI. Because
the .deb builds its venv from the *system* python3, the end user's minor
version picks the wheel, which is why wheels/ used to carry all five of
cp310..cp314. That is 2.3 MB of near-identical binary blobs -- HALF the shipped
package -- for a module that is only ever imported by `flet` as its
serialisation codec (`msgpack.packb` / `msgpack.unpackb` / `msgpack.ExtType`).

Every one of those wheels already contains msgpack's own PURE-PYTHON
implementation, `msgpack/fallback.py`, and msgpack/__init__.py selects between
them explicitly:

    if os.environ.get("MSGPACK_PUREPYTHON"):
        from .fallback import Packer, Unpacker, unpackb
    else:
        try:
            from ._cmsgpack import Packer, Unpacker, unpackb
        except ImportError:
            from .fallback import Packer, Unpacker, unpackb

So the fallback is an upstream-supported runtime mode, not a patch: if the
extension is absent, msgpack imports and works. This script builds a wheel that
is exactly the union of the ABI-independent files from the upstream wheels,
with the `.so` left out -- one 15 KB wheel that installs on 3.10 through 3.14
instead of five 460 KB wheels.

WHAT THIS DOES NOT DO
---------------------
It does not touch a single byte of upstream code. `msgpack/__init__.py`,
`ext.py`, `fallback.py`, `exceptions.py`, METADATA, top_level.txt and the
licence file are copied verbatim; this script asserts they are byte-identical
across every upstream wheel it is given, so the result is the one artifact all
of them agree on rather than one ABI's private view. Only `WHEEL` (the tag) and
`RECORD` (hashes of what is actually inside) are regenerated, because those
describe the wheel rather than the code.

That means the resulting hash is NOT the sha256 published on PyPI -- no such
artifact exists upstream. `requirements-runtime.lock` says so explicitly, next
to pyaes, which the project already builds by hand because PyPI ships it as an
sdist only. scripts/build-deb.sh re-checks the shape of the vendored wheel on
every build so this cannot silently rot back into a binary one.

USAGE (needs network, run by hand when bumping msgpack)
-------------------------------------------------------
    mkdir -p /tmp/mp && pip download --only-binary :all: --no-deps -d /tmp/mp \
        --platform manylinux2014_x86_64 \
        'msgpack==1.2.3' || for v in 310 311 312 313 314; do
          pip download --only-binary :all: --no-deps -d /tmp/mp \
            --python-version 3.$((10#$v - 300)) --implementation cp --abi cp$v \
            --platform manylinux2014_x86_64 'msgpack==1.2.3'
        done
    python3 scripts/vendor_msgpack_purepython.py wheels /tmp/mp

Then paste the printed sha256 into requirements-runtime.lock and regenerate
wheels/SHA256SUMS.
"""
import base64
import glob
import hashlib
import os
import sys
import zipfile

# Fixed so the output is reproducible: the same upstream bytes always produce
# the same sha256, which is what lets the lock pin it.
ZIP_DATE = (1980, 1, 1, 0, 0, 0)


def die(msg):
    sys.stderr.write(f"vendor_msgpack_purepython.py: {msg}\n")
    sys.exit(1)


def sha256_urlsafe(data):
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def parse_name(filename):
    """msgpack-1.2.3-cp312-cp312-manylinux....whl -> (name, version)."""
    stem = filename[: -len(".whl")]
    parts = stem.split("-")
    if len(parts) < 3:
        die(f"cannot parse wheel filename: {filename}")
    return parts[0], parts[1]


def collect(source_dir):
    """Return (version, {member: bytes}) asserting provenance across all wheels."""
    wheels = sorted(glob.glob(os.path.join(source_dir, "msgpack-*.whl")))
    if not wheels:
        die(f"no msgpack-*.whl in {source_dir}")

    # RECORD and WHEEL are excluded from the provenance check on purpose: they
    # DESCRIBE the wheel, so they legitimately differ per ABI (WHEEL carries the
    # cp31x tag, RECORD lists the .so). Both are regenerated below. Every other
    # member is code, or metadata about the code, and must match everywhere.
    per_abi = ("RECORD", "WHEEL")

    versions, per_member = set(), {}
    for path in wheels:
        name, version = parse_name(os.path.basename(path))
        if name != "msgpack":
            die(f"not a msgpack wheel: {path}")
        versions.add(version)
        with zipfile.ZipFile(path) as zf:
            for member in zf.namelist():
                if member.endswith("/") or "_cmsgpack" in member:
                    continue  # directory entries and the compiled object are dropped
                if member.rsplit("/", 1)[-1] in per_abi:
                    continue
                per_member.setdefault(member, {})[os.path.basename(path)] = hashlib.sha256(
                    zf.read(member)
                ).hexdigest()

    if len(versions) != 1:
        die(f"upstream wheels disagree on version: {sorted(versions)}")
    version = versions.pop()

    # Provenance: every ABI must ship the SAME pure-python files, otherwise the
    # wheel we synthesise would be one ABI's opinion about them.
    disagreements = {
        member: per for member, per in per_member.items() if len(set(per.values())) != 1
    }
    if disagreements:
        for member, per in sorted(disagreements.items()):
            sys.stderr.write(f"  {member}: {per}\n")
        die("upstream wheels disagree on their ABI-independent files; refusing to guess")

    files = {}
    with zipfile.ZipFile(wheels[0]) as zf:
        for member in per_member:
            files[member] = zf.read(member)

    if "msgpack/fallback.py" not in files:
        die("upstream wheel has no msgpack/fallback.py; it can no longer run "
            "without the C extension, so vendoring it this way would be a "
            "silent downgrade. Re-vendor the binary wheels instead.")
    if "msgpack/__init__.py" not in files:
        die("upstream wheel has no msgpack/__init__.py")
    return version, files


def build(out_dir, version, files):
    dist_info = f"msgpack-{version}.dist-info"
    files = dict(files)
    # Root-Is-Purelib/Tag are the only fields that can change: the files
    # themselves are ABI-independent, which is the whole premise.
    files[f"{dist_info}/WHEEL"] = (
        "Wheel-Version: 1.0\n"
        "Generator: vendor_msgpack_purepython.py (upstream Generator: setuptools)\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    ).encode()

    record = []
    for member in sorted(files):
        record.append(f"{member},sha256={sha256_urlsafe(files[member])},{len(files[member])}")
    record.append(f"{dist_info}/RECORD,,")  # RECORD never hashes itself
    files[f"{dist_info}/RECORD"] = ("\n".join(record) + "\n").encode()

    out = f"msgpack-{version}-py3-none-any.whl"
    path = os.path.join(out_dir, out)
    # Written straight into out_dir rather than built in $CWD and moved:
    # os.replace() cannot cross a filesystem boundary, and this script is
    # routinely run with /tmp for the download and the repo for the output.
    buf = zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9)
    with buf:
        for member in sorted(files):
            info = zipfile.ZipInfo(member, date_time=ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o644 << 16) | 0  # plain file, regular mode
            info.create_system = 3  # unix, so the mode bits above are honoured
            buf.writestr(info, files[member])
    return path


def verify(path):
    """Re-open the wheel we just wrote and hold it to the same rules."""
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        if any("_cmsgpack" in n for n in names):
            die("output still contains a compiled extension")
        if "msgpack/fallback.py" not in names:
            die("output is missing msgpack/fallback.py")
        wheel = zf.read(next(n for n in names if n.endswith(".dist-info/WHEEL"))).decode()
        if "Tag: py3-none-any" not in wheel or "Root-Is-Purelib: true" not in wheel:
            die(f"output is not tagged py3-none-any:\n{wheel}")
        record = zf.read(next(n for n in names if n.endswith(".dist-info/RECORD"))).decode()
        listed = set()
        for line in record.splitlines():
            member, _, rest = line.partition(",")
            digest, _, size = rest.partition(",")
            listed.add(member)
            if not digest:  # the RECORD row itself
                continue
            data = zf.read(member)
            if digest != "sha256=" + sha256_urlsafe(data) or int(size) != len(data):
                die(f"RECORD does not match the bytes of {member}")
        if listed != set(names):
            die(f"RECORD and wheel contents disagree: {listed ^ set(names)}")


def main():
    if len(sys.argv) != 3:
        sys.stderr.write(__doc__)
        sys.exit(2)
    out_dir, source_dir = sys.argv[1], sys.argv[2]
    version, files = collect(source_dir)
    path = build(out_dir, version, files)
    verify(path)

    with open(path, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    size = os.path.getsize(path)
    print(f"wrote {path}")
    print(f"  {len(files)} members, {size} bytes")
    print(f"  sha256:{digest}")
    print()
    print("requirements-runtime.lock:")
    print(f'msgpack=={version} \\')
    print(f"    --hash=sha256:{digest}")


if __name__ == "__main__":
    main()
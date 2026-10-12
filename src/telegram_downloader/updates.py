"""In-app update checker.

Mirrors simple-yt-downloader's update_checker.dart: query the GitHub
releases API, compare versions, and pick the right installer asset for
the current platform. Stdlib only (urllib) so the CLI stays dependency-free.

Release-asset naming convention (see scripts/build-*.sh + CI):
    telegram-downloader_<ver>_amd64.deb   Linux
    telegram-downloader_<ver>.dmg         macOS
    telegram-downloader_<ver>.apk         Android
Each platform job also publishes SHA256SUMS-<plat> (deb/apk/dmg) plus a
detached Ed25519 signature SHA256SUMS-<plat>.sig over that manifest.
download_asset() verifies BOTH before any installer byte is fetched: the
signature against a public key pinned in _release_verify.py (second trust
anchor), then the payload against the signed manifest. Everything fails
closed — see download_asset().
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from . import _release_verify as rv

REPO = "MDHasan0078/telegram-downloader"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"

# Where installer/release URLs may point. Anything else (file://, intranet,
# lookalike hosts) is refused before opening or downloading.
RELEASE_HOSTS = {"github.com"}
# Hosts the JSON API call (and its redirects) may touch. Kept separate from
# the asset set: the API must never be used to serve release bytes, and API
# redirects used to fail closed because api.github.com was missing here.
API_HOSTS = {"api.github.com", "github.com"}
# Redirect allowlist for release bytes only: github.com/.../releases/download
# really does 302 to the CDN, and those CDN hosts are never used directly for
# an API call. Asset URLs are additionally REPO-pinned (see
# _REPO_RELEASE_ASSET_RE) — a host allowlist alone would happily serve another
# account's release.
ASSET_HOSTS = {"github.com", "objects.githubusercontent.com",
               "release-assets.githubusercontent.com"}
MAX_ASSET_BYTES = 800 * 1024 ** 2
# Releases-API JSON. A real release lists a handful of assets (~8 KiB); this
# is deliberately ~30x that, and still small enough that a hostile endpoint
# cannot use the update check as an unbounded memory sink.
MAX_RELEASE_JSON_BYTES = 256 * 1024


@dataclass
class UpdateAsset:
    name: str
    url: str


@dataclass
class UpdateInfo:
    latest_version: str  # without leading 'v'
    release_url: str
    assets: list[UpdateAsset] = field(default_factory=list)

    def is_newer_than(self, current: str) -> bool:
        def parts(v: str) -> list[int]:
            out = []
            for piece in v.strip().lstrip("v").split("."):
                m = re.match(r"(\d+)", piece)
                out.append(int(m.group(1)) if m else 0)
            return out
        cur, lat = parts(current or "0"), parts(self.latest_version)
        for i in range(max(len(cur), len(lat))):
            a = lat[i] if i < len(lat) else 0
            b = cur[i] if i < len(cur) else 0
            if a != b:
                return a > b
        return False

    def asset_for_platform(self, operating_system: str | None = None) -> UpdateAsset | None:
        """Best installer asset for this OS (exact versioned name only).

        No suffix fallback: accepting any `*.deb` lets a malicious release
        smuggle an unrelated payload that the UI would present as trusted.
        """
        if operating_system is None:
            operating_system = platform.system().lower()
            if operating_system == "linux" and "com.termux" in os.environ.get("PREFIX", ""):
                operating_system = "android"
        if operating_system in ("darwin", "macos"):
            versioned = {f"telegram-downloader_{self.latest_version}.dmg"}
        elif operating_system == "linux":
            versioned = {f"telegram-downloader_{self.latest_version}_amd64.deb"}
        elif operating_system == "android":
            versioned = {f"telegram-downloader_{self.latest_version}.apk"}
        else:
            return None
        for asset in self.assets:
            if asset.name in versioned:
                return asset
        return None


def _https_url_ok(url: str, hosts: set[str]) -> bool:
    """Allowlist check: https only, exact host match (no userinfo/ports tricks).

    Raw control characters / whitespace in the URL string are refused up
    front. They cannot reach us today (http.client rejects them), but allowing
    them in a security check is how parser-differential bugs are born: what we
    validate must be exactly what gets opened.
    """
    try:
        if not isinstance(url, str) or not url:
            return False
        if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
            return False
        parts = urllib.parse.urlparse(url)
        return (parts.scheme == "https" and parts.hostname in hosts
                and not parts.username and not parts.password
                and parts.port in (None, 443))
    except Exception:
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects ONLY to allowlisted hosts; refuse everything else so
    the allowlist can't be bypassed by a 302 from an allowlisted host to an
    attacker URL (SSRF/downgrade). GitHub release downloads always redirect
    `github.com/.../releases/download/...` -> release-assets.githubusercontent.com,
    so blanket-blocking 302s was breaking the in-app updater.

    Auth: if the redirect stays on the same host the Authorization header is
    kept; on a cross-host (allowlisted) redirect the token is dropped so it
    never leaks to a third-party CDN.
    """

    def __init__(self, allowed_hosts: set[str] | None = None):
        super().__init__()
        self._allowed = allowed_hosts or ASSET_HOSTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            parts = urllib.parse.urlparse(newurl)
        except Exception:
            return None
        if (parts.scheme not in ("https",) or parts.hostname not in self._allowed
                or parts.username or parts.password
                or parts.port not in (None, 443)):
            return None
        keep_auth = (str(req.host).split(":", 1)[0].lower() == parts.hostname)
        hdrs = {k: v for k, v in req.headers.items() if k.lower() not in ("authorization",)}
        if keep_auth and "Authorization" in req.headers:
            hdrs["Authorization"] = req.headers["Authorization"]
        return urllib.request.Request(newurl, headers=hdrs,
                                      data=req.data, method=req.get_method())


def _urlopen_no_redirect(req: urllib.request.Request, timeout: int,
                         token: str | None = None,
                         allowed_hosts: set[str] | None = None):
    """Open *req* with proxies off and allowlisted-only redirects.

    Token policy: no credential is sent on the first attempt (nothing to leak
    if this ever points at the wrong host). A token is retried exactly once,
    and only when GitHub answers a status a token can actually help with.
    The token is never logged and never appears in an error message.

    404 belongs in that retry set. While this repository was PRIVATE, GitHub
    answered 404 -- not 403 -- to an unauthenticated request for it,
    deliberately, so that its existence would not be disclosed. Retrying only
    on 403/429 therefore meant the updater could NEVER read its own releases:
    verified with a valid token, the same request succeeded via curl and gh
    while check_for_update() returned None.

    Keeping 404 in the set is correct now that the repo is PUBLIC too: the
    unauthenticated first request then simply succeeds and the retry never
    fires, so the behaviour is unchanged -- but a user who has since made the
    repo private again, or who runs a fork, is still covered. The retry still
    sends the token only to an allowlisted host, so this widens *when* a
    token is used, not
    *where* it can go.
    """
    allowed = ASSET_HOSTS if allowed_hosts is None else allowed_hosts

    def _open(with_token: str | None):
        headers = {k: v for k, v in req.headers.items()
                   if k.lower() != "authorization"}
        if with_token:
            headers["Authorization"] = f"Bearer {with_token}"
        request = urllib.request.Request(req.full_url, headers=headers,
                                         data=req.data,
                                         method=req.get_method())
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                             _NoRedirect(allowed))
        return opener.open(request, timeout=timeout)

    try:
        return _open(None)
    except urllib.error.HTTPError as exc:
        # 403/429 = rate limited or forbidden; 404 = the private-repo
        # concealment status. Both are "try once with a credential".
        if token and exc.code in (403, 404, 429):
            exc.close()
            return _open(token)
        raise


def _read_capped(resp, cap: int, what: str) -> bytes:
    """Read at most *cap* bytes from *resp*, raising if the body is larger.

    Every other body in this module is capped (MAX_CHECKSUM_BYTES, the
    signature, the payload); the releases-API JSON used to be the one read
    that a hostile or hijacked endpoint could stream into memory without
    bound. Refuse on a declared oversize *before* buffering anything, then
    ask for one byte more than the cap and reject if it came back.

    Tolerates response-like objects that do not expose `headers` or whose
    `read()` takes no size argument (stand-ins, some wrappers): both only
    remove the early/length hints, the bounded `read(cap + 1)` is what
    actually enforces the limit — and every real urllib response accepts
    the argument.
    """
    headers = getattr(resp, "headers", None)
    declared = headers.get("Content-Length") if headers is not None else None
    if declared is not None:
        try:
            declared_size = int(declared)
        except (TypeError, ValueError):
            raise ValueError(f"Bad Content-Length in {what}: {declared!r}")
        if declared_size > cap:
            raise ValueError(
                f"{what} too large (declared {declared_size} bytes); refusing.")
    try:
        body = resp.read(cap + 1)
    except TypeError:
        body = resp.read()
    if len(body) > cap:
        raise ValueError(f"{what} exceeded the {cap}-byte cap; refusing.")
    return body


_VERSION_RE = re.compile(r"^v?[0-9]+\.[0-9]+\.[0-9]+\Z")
# Bound to our repo: any other owner/repo must not open as a trusted update.
# Use \Z not $ to block trailing \n injection. ASCII [0-9] only — never \d,
# whose Unicode digit set would let a non-ASCII "version" match.
_RELEASE_URL_RE = re.compile(
    rf"^https://github\.com/{re.escape(REPO)}/releases/tag/v?[0-9]+\.[0-9]+\.[0-9]+\Z")
# REPO-pinned asset location. A host allowlist is NOT enough: github.com hosts
# every account's releases, so an allowlisted attacker repo could hand us its
# own installer under a name we already trust. The path prefix is the binding
# between payload and release; the checksum/signature is the second binding.
_REPO_RELEASE_ASSET_RE = re.compile(
    rf"^https://github\.com/{re.escape(REPO)}/releases/download/"
    r"v[0-9]+\.[0-9]+\.[0-9]+/[A-Za-z0-9][A-Za-z0-9._+-]*\Z")
# Version-bound, case-sensitive, no traversal: only our exact release
# filenames are ever downloaded. \Z blocks trailing newline bypass.
# (No .exe: there is no Windows build job.)
_ASSET_NAME_RE = re.compile(
    r"^telegram-downloader_[0-9]+\.[0-9]+\.[0-9]+(_amd64)?\.(deb|dmg|apk)\Z")

# Per-platform checksum files published alongside the installers.
# Exact names only — never fetched from an untrusted listing.
_CHECKSUM_NAMES = {
    "linux": "SHA256SUMS-deb",
    "android": "SHA256SUMS-apk",
    "darwin": "SHA256SUMS-dmg",
    "macos": "SHA256SUMS-dmg",
}
_CHECKSUM_NAME_RE = re.compile(r"^SHA256SUMS-(deb|apk|dmg)\Z")
MAX_CHECKSUM_BYTES = 64 * 1024

# --- U4 anti-downgrade / plausibility: DISABLED (see commit ce7e0fd) -------
# The plausible-jump bounds and the last-seen-version replay memory were
# removed to match simple-yt-downloader's simpler updater. They are recoverable
# with `git show ce7e0fd`, whose message records what was kept and why; that
# history is a better record than commented-out code, which cannot be
# half-restored and silently rots.
#
# What this costs: a hostile API response can now offer an absurd but
# well-formed tag such as v99.99.99, and there is no memory of the highest
# version previously offered, so a replayed OLDER-but-genuinely-signed release
# is not refused (UpdateInfo.is_newer_than compares against the INSTALLED
# version, not against what was last shown). What it does NOT cost: install
# safety. download_asset() still verifies the Ed25519 manifest signature and
# the payload digest before any installer byte is fetched, and
# tests/test_structural_regressions.py pins that ordering.


def _parse_version(value: str) -> tuple[int, int, int] | None:
    m = re.fullmatch(r"v?([0-9]+)\.([0-9]+)\.([0-9]+)", (value or "").strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def check_for_update(repo: str = REPO, timeout: int = 10,
                     token: str | None = None) -> UpdateInfo | None:
    """Return UpdateInfo or None (offline / no releases / rate-limited).
    Never raises.

    This function does NOT compare versions. It fetches the latest release,
    validates the tag shape, the host, the repo-pinned asset URLs and the
    asset filenames, and returns whatever the API said. Deciding whether that
    is actually an update is the CALLER's job, via UpdateInfo.is_newer_than()
    — a component-wise numeric compare, so 2.10.0 correctly beats 2.9.9
    (gui.py's settings screen and cli.py's `update` command each call it).

    Consequence worth stating plainly, since this is where the removed U4
    guards used to act: an absurd but well-formed tag such as v99.99.99 from a
    hijacked API is returned, not suppressed, and there is no memory of the
    highest version previously offered, so a replayed OLDER-but-signed release
    is not refused here. That does not weaken install safety — the tag must
    still be v<semver> on this repo's releases, and download_asset() verifies
    the Ed25519 manifest signature and the payload digest before any byte is
    installed.
    """
    if repo != REPO:
        return None
    if token is None:
        token = os.environ.get("GITHUB_TOKEN") or ""
    try:
        headers: dict[str, str] = {
            "User-Agent": "telegram-downloader",
            "Accept": "application/vnd.github+json",
        }
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/releases/latest",
            headers=headers,
        )
        with _urlopen_no_redirect(req, timeout=timeout,
                                  token=token or None,
                                  allowed_hosts=API_HOSTS) as resp:
            if resp.status != 200:
                return None
            # Size-capped like every other body read here: the release JSON
            # arrives from the network and must not be an unbounded sink.
            data = json.loads(_read_capped(resp, MAX_RELEASE_JSON_BYTES,
                                           "release metadata")
                              .decode("utf-8"))
        assets = []
        for a in (data.get("assets") or []):
            raw_url = a.get("browser_download_url") or ""
            raw_name = a.get("name") or ""
            # Ignore assets whose browser_download_url isn't on our host
            # allowlist, isn't under OUR repo's release path, or whose name
            # isn't version-bound — a compromised release can't smuggle a
            # cross-repo payload past a host-only check.
            if not _https_url_ok(raw_url, ASSET_HOSTS):
                continue
            if not _REPO_RELEASE_ASSET_RE.match(raw_url.split("?", 1)[0]
                                                .split("#", 1)[0]):
                continue
            url_base = raw_url.split("?", 1)[0].split("#", 1)[0]
            url_name = url_base.rsplit("/", 1)[-1] if "/" in url_base else url_base
            if not _ASSET_NAME_RE.match(raw_name or ""):
                continue
            if not _ASSET_NAME_RE.match(url_name or ""):
                continue
            if url_name != raw_name:
                continue
            assets.append(UpdateAsset(name=raw_name, url=raw_url))
        tag = str(data.get("tag_name") or "").strip()
        if not tag or not _VERSION_RE.match(tag):
            return None
        release_url = str(data.get("html_url") or "")
        if not _RELEASE_URL_RE.match(release_url):
            return None
        # url_name == raw_name already enforced for every kept asset.
        # Keep only the exact versioned filename for that tag's version.
        # removeprefix is charset-safe unlike lstrip("vV")
        tag_ver = tag.removeprefix("v").removeprefix("V") if hasattr(tag, "removeprefix") else tag.lstrip("vV")
        # second strip handles Vv/vV oddities but removeprefix already strict
        if tag_ver.startswith(("v","V")):
            tag_ver = tag_ver[1:]
        latest_version = tag.lstrip("v").lstrip("V")
        expected = {
            f"telegram-downloader_{tag_ver}_amd64.deb",
            f"telegram-downloader_{tag_ver}.dmg",
            f"telegram-downloader_{tag_ver}.apk",
        }
        assets = [a for a in assets if a.name in expected]
        return UpdateInfo(latest_version=latest_version,
                          release_url=release_url,
                          assets=assets)
    except Exception:
        return None


def _checksum_name_for(asset_name: str) -> str | None:
    if asset_name.endswith(".deb"):
        return "SHA256SUMS-deb"
    if asset_name.endswith(".apk"):
        return "SHA256SUMS-apk"
    if asset_name.endswith(".dmg"):
        return "SHA256SUMS-dmg"
    return None


def _fetch_release_file(name: str, tag_version: str, timeout: int = 30,
                        token: str | None = None,
                        max_bytes: int = MAX_CHECKSUM_BYTES,
                        optional: bool = False) -> bytes | None:
    """Download one file from OUR repo's release assets, size-capped.

    The URL is built from the REPO/RELEASE constants, never from anything the
    API handed us, and is re-validated against the repo-pinned path pattern.

    Fail-closed: any problem raises. `optional=True` turns only "404 Not Found"
    into None (used for the detached signature, whose absence is meaningful);
    every other failure still raises.
    """
    if not _VERSION_RE.match(f"v{tag_version}"):
        raise ValueError(f"Refusing unexpected tag version: {tag_version!r}")
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError(f"Refusing unexpected release file name: {name!r}")
    url = (f"https://github.com/{REPO}/releases/download/"
           f"v{tag_version}/{name}")
    if not _https_url_ok(url, ASSET_HOSTS) or not _REPO_RELEASE_ASSET_RE.match(url):
        raise ValueError(f"Refusing to fetch release file from untrusted URL: {url!r}")
    headers = {"User-Agent": "telegram-downloader"}
    req = urllib.request.Request(url, headers=headers)
    try:
        with _urlopen_no_redirect(req, timeout=timeout, token=token or None) as resp:
            if resp.status != 200:
                raise ValueError(
                    f"Release file unavailable (HTTP {resp.status}); refusing.")
            declared = resp.headers.get("Content-Length")
            if declared is not None:
                try:
                    if int(declared) > max_bytes:
                        raise ValueError("Release file too large; refusing.")
                except ValueError:
                    raise
                except (TypeError, AttributeError) as exc:
                    raise ValueError(f"Bad Content-Length: {declared!r}") from exc
            body = resp.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        if optional and exc.code == 404:
            return None
        raise
    if len(body) > max_bytes:
        raise ValueError("Release file too large; refusing.")
    return body


def _parse_sums(body: bytes) -> dict[str, str]:
    """Parse a SHA256SUMS file into {filename: hexdigest}.

    Only version-bound installer names are kept, so a manifest can never
    steer verification at an arbitrary path.
    """
    out: dict[str, str] = {}
    for line in body.decode("utf-8", errors="strict").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2:
            continue
        digest, fname = parts
        if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            continue
        fname = fname.lstrip("*")
        if _ASSET_NAME_RE.match(fname):
            out[fname] = digest.lower()
    if not out:
        raise ValueError("Checksum file has no usable entries; refusing unverified installer.")
    return out


# An unsigned {filename: digest} mapping that REFUSES to be used as if it were
# verified. _fetch_checksum() used to return a bare dict, indistinguishable
# from _fetch_verified_sums()'s return value: `sums = _fetch_checksum(...)`
# reads exactly like `sums = _fetch_verified_sums(...)` and only a comment
# said not to use it. A future caller who skims past the comment gets TLS-only
# trust for an installer and never notices. The type now makes the mistake loud
# (no .get(), so `sums.get(name)` raises) and `verified` is False so nothing
# can assert it is true.
class UnverifiedSums(Mapping):
    """READ-ONLY, SIGNATURE-CHECK-NOT-PERFORMED digest mapping.

    An instance proves nothing about who published the release. Do NOT use it
    to gate an installer download — use _fetch_verified_sums(), which
    verifies the manifest's Ed25519 signature before returning a plain dict.
    """

    verified = False

    def __init__(self, sums: dict[str, str]):
        self._sums = dict(sums)

    def __getitem__(self, key):
        return self._sums[key]

    def __iter__(self):
        return iter(self._sums)

    def __len__(self):
        return len(self._sums)

    def __repr__(self):
        return (f"UnverifiedSums(UNVERIFIED, do-not-install-from: "
                f"{sorted(self._sums)})")

    def __bool__(self):
        return bool(self._sums)

    def get(self, *_a, **_k):  # noqa: D102
        raise AttributeError(
            "UnverifiedSums has no .get(): these digests are not signature-"
            "verified and must not gate an install. Use "
            "_fetch_verified_sums(), which verifies the manifest signature.")


def _fetch_checksum(checksum_name: str, tag_version: str, timeout: int = 30,
                    token: str | None = None) -> "UnverifiedSums":
    """Download SHA256SUMS-<plat> UNVERIFIED. Diagnostic use only.

    *** THIS DOES NOT VERIFY THE MANIFEST'S SIGNATURE. ***
    *** NEVER use the result to decide whether to install a binary. ***

    Returns an UnverifiedSums, not a dict, precisely so that a caller cannot
    mistake it for _fetch_verified_sums()'s verified result: it has no .get(),
    its repr shouts UNVERIFIED, and UnverifiedSums.verified is False. If you
    are installing anything, you want _fetch_verified_sums().

    Fail-closed: any problem raises — a compromised/mirrored release must
    never silently skip verification.
    """
    if not _CHECKSUM_NAME_RE.match(checksum_name or ""):
        raise ValueError(f"Refusing unexpected checksum name: {checksum_name!r}")
    body = _fetch_release_file(checksum_name, tag_version, timeout=timeout,
                               token=token, max_bytes=MAX_CHECKSUM_BYTES)
    return UnverifiedSums(_parse_sums(body))


def _fetch_verified_sums(checksum_name: str, tag_version: str, timeout: int = 30,
                         token: str | None = None,
                         allow_unsigned: bool = False) -> dict[str, str]:
    """Fetch the manifest for *tag_version* and verify its Ed25519 signature.

    This is the second trust anchor: the manifest is fetched from the same
    place as the payload, so it is only worth anything once its signature
    checks out against a key pinned in _release_verify.py.

    Fail-closed. Raises rv.MissingSignatureError (unsigned release),
    rv.UnknownSigningKeyError (signed by a key we do not pin),
    rv.NoPinnedSigningKeyError (this build pins no key),
    rv.SignatureFormatError or rv.BadSignatureError (tampered manifest).
    `allow_unsigned=True` is the explicit, clearly-named opt-out — it only
    applies when NO signature is published; a signature that is present but
    does not verify is always fatal, otherwise an attacker could attach
    garbage to push the verifier onto the unsigned path.
    """
    if not _CHECKSUM_NAME_RE.match(checksum_name or ""):
        raise ValueError(f"Refusing unexpected checksum name: {checksum_name!r}")
    manifest = _fetch_release_file(checksum_name, tag_version, timeout=timeout,
                                   token=token, max_bytes=MAX_CHECKSUM_BYTES)
    signature = _fetch_release_file(checksum_name + rv.SIGNATURE_SUFFIX,
                                    tag_version, timeout=timeout, token=token,
                                    max_bytes=rv.MAX_SIGNATURE_BYTES,
                                    optional=True)
    if signature is None and allow_unsigned:
        return _parse_sums(manifest)
    rv.verify_manifest_signature(manifest, signature)
    return _parse_sums(manifest)


def _verify_file_sha256(path, digest: str) -> None:
    """SHA256-verify a file by opening it through O_NOFOLLOW and hashing the
    held fd. Hashing by path lets a swap between our hash and the caller's
    publish/browse introduce tampered bytes; an fd-based hash verifies the
    exact inode we are about to publish."""
    import os as _os
    import hashlib
    import hmac as _hmac
    nofollow = getattr(_os, "O_NOFOLLOW", 0)
    fd = _os.open(path, _os.O_RDONLY | nofollow)
    try:
        h = hashlib.sha256()
        while True:
            chunk = _os.read(fd, 1024 * 256)
            if not chunk:
                break
            h.update(chunk)
        if not _hmac.compare_digest(h.hexdigest(), digest.lower()):
            raise ValueError("SHA256 mismatch — installer may be tampered; refusing.")
    finally:
        try:
            _os.close(fd)
        except OSError:
            pass


def _unlink_quietly(path) -> None:
    """Remove *path*, never raising and never following a symlink.

    Used only for files this module just created. os.unlink() removes the
    directory entry itself, so a planted symlink named like our temp file
    loses the link, not whatever it points at. Best-effort on purpose: a
    cleanup failure must not replace the real error (or the return value)
    with a secondary one.
    """
    try:
        os.unlink(path)
    except OSError:
        pass


# Exactly the shape download_asset() mints: <installer>.part.<pid>.<16 hex>.
# Derived from _ASSET_NAME_RE (anchor stripped) so it can never drift away
# from the names we actually create. Nothing else in a download folder is
# ever a candidate for deletion.
_STALE_PART_RE = re.compile(
    _ASSET_NAME_RE.pattern[:-len(r"\Z")]
    + r"\.part\.[0-9]{1,10}\.[0-9a-f]{16}\Z")
# A leftover younger than this may belong to an updater that is running
# *right now* in another window/process, so it is never touched.
STALE_PART_MIN_AGE = 3600.0
# At most this many directory entries are examined per sweep, and at most one
# sweep per this many seconds: the sweep runs inside download_asset() and must
# not turn every update check into a directory walk.
STALE_SWEEP_INTERVAL = 300.0
STALE_SWEEP_MAX_ENTRIES = 64
_last_stale_sweep = 0.0


def _sweep_stale_parts(directory) -> None:
    """Delete abandoned `<installer>.part.*` files left by killed runs.

    download_asset() cleans up after itself, but a SIGKILL, a power cut or a
    process killed by the OS leaves the partial file in the user's Downloads
    folder, where a later "open folder" step can offer them a binary that was
    never verified. Safety rules, all of them deliberate:

      * only names matching our own `.part.<pid>.<hex>` shape are considered,
        so an unrelated user file is never a candidate;
      * symlinks are never followed and never unlinked-through (we skip them);
      * anything younger than STALE_PART_MIN_AGE is left alone, so a
        concurrent download in another process is never truncated;
      * bounded by entry count, time-based rate-limited, and every single
        error is swallowed — a sweep can never fail a download.
    """
    global _last_stale_sweep
    import time as _time
    # NB: wall clock, not monotonic — it is compared against st_mtime.
    now = _time.time()
    if now - _last_stale_sweep < STALE_SWEEP_INTERVAL:
        return
    _last_stale_sweep = now  # set first: a failing sweep still rate-limits
    try:
        with os.scandir(str(directory)) as entries:
            for i, entry in enumerate(entries):
                if i >= STALE_SWEEP_MAX_ENTRIES:
                    return
                if not _STALE_PART_RE.match(entry.name):
                    continue
                try:
                    # follow_symlinks=False everywhere: a planted symlink is
                    # neither a candidate nor a thing we read through.
                    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                        continue
                    if now - entry.stat(follow_symlinks=False).st_mtime < STALE_PART_MIN_AGE:
                        continue
                    os.unlink(entry.path)
                except OSError:
                    continue
    except OSError:
        return


def open_release_page(url: str) -> None:
    if not _https_url_ok(url, RELEASE_HOSTS) or not _RELEASE_URL_RE.match(url):
        raise ValueError(f"Refusing to open untrusted URL: {url!r}")
    # Resolve openers to absolute paths: a hostile $PATH must not swap
    # the browser launcher between check and exec.
    import shutil as _shutil
    if sys.platform.startswith("linux"):
        exe = _shutil.which("xdg-open")
        if not exe:
            raise ValueError("xdg-open not found; open the URL manually.")
        subprocess.Popen([exe, url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    elif sys.platform == "darwin":
        exe = _shutil.which("open") or "/usr/bin/open"
        subprocess.Popen([exe, url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    elif sys.platform == "win32":
        # NOTE: list-form Popen does NOT protect cmd.exe metachars, so the
        # strict allowlist above is load-bearing here. webbrowser avoids cmd.
        import webbrowser
        webbrowser.open(url)
    else:
        print(url)


_UNSET = object()


def _resolve_legacy_args(legacy_args: tuple, tag_version, token):
    """Accept (only) gui.py's historical positional call shape.

    download_asset(asset, dir, version) / download_asset(asset, dir, version,
    token). Anything else is a TypeError, so the keyword-only contract is not
    quietly widened to "any number of positional arguments".
    """
    if len(legacy_args) > 2:
        raise TypeError("download_asset() takes at most 4 positional arguments")
    if len(legacy_args) >= 1:
        if tag_version is not _UNSET:
            raise TypeError("download_asset() got multiple values for 'tag_version'")
        tag_version = legacy_args[0]
    if len(legacy_args) == 2:
        if token is not None:
            raise TypeError("download_asset() got multiple values for 'token'")
        token = legacy_args[1]
    return tag_version, token


def download_asset(asset: UpdateAsset, dest_dir, *legacy_args,
                   tag_version=_UNSET,
                   token: str | None = None,
                   allow_unsigned: bool = False,
                   timeout: int = 30) -> object:
    """Download an installer asset into *dest_dir*. Returns the final path.

    Integrity (both steps mandatory, both before a single payload byte is
    fetched — see U1/U2):

      1. tag_version is REQUIRED. There is no default: omitting it, passing
         None or passing "" is an error, so "verification" can never be
         silently skipped by a caller that forgot the argument. The manifest
         is fetched and its Ed25519 signature verified against a key pinned in
         _release_verify.py, before the download starts.
      2. The downloaded installer must hash to the digest in that verified
         manifest. The published file is re-verified after the rename.

    `allow_unsigned=True` is the explicit, clearly-named opt-in for releases
    that publish no signature at all (self-hosted/dev builds). A signature
    that is present but does not verify is always fatal.

    Also hardened (unchanged): https + repo-pinned asset path + asset-host
    redirect allowlist, no-redirect fetch, strict release filename, sanitized
    name, size cap, symlink-safe atomic write. Never executes the result.
    """
    # gui.py (owned by another change-set) still calls this positionally:
    # download_asset(asset, dir, info.latest_version, token). Rather than
    # leaving a default value behind — the one thing that made "no
    # verification" reachable — accept exactly that legacy shape.
    tag_version, token = _resolve_legacy_args(legacy_args, tag_version, token)
    if tag_version is _UNSET:
        raise TypeError(
            "download_asset() missing required keyword-only argument: "
            "'tag_version' — the release version is needed to fetch and "
            "verify the signed checksum manifest; refusing unverified "
            "installer.")
    if not isinstance(tag_version, str) or not tag_version.strip():
        raise ValueError(
            f"Refusing to install without a release version: {tag_version!r}")
    if not _VERSION_RE.match(f"v{tag_version}"):
        raise ValueError(f"Refusing unexpected tag version: {tag_version!r}")
    import os as _os
    import secrets as _secrets
    from .config import harden_private_file as _harden
    from .telegram_utils import safe_filename
    if token is None:
        token = os.environ.get("GITHUB_TOKEN") or ""
    if not _https_url_ok(asset.url, ASSET_HOSTS):
        raise ValueError(f"Refusing to download from untrusted URL: {asset.url!r}")
    base_url = asset.url.split("?", 1)[0].split("#", 1)[0]
    if not _REPO_RELEASE_ASSET_RE.match(base_url):
        raise ValueError(
            f"Refusing to download from outside {REPO}'s releases: "
            f"{asset.url!r}")
    # Bind the payload to the release whose manifest we are about to fetch:
    # otherwise a v1.0.0 asset could be checked against a (hostile) manifest
    # that happens to list a v1.0.0 filename.
    if not base_url.startswith(
            f"https://github.com/{REPO}/releases/download/v{tag_version}/"):
        raise ValueError(
            f"Asset URL is not from release v{tag_version}: {asset.url!r}")
    if not _ASSET_NAME_RE.match(asset.name or ""):
        raise ValueError(f"Refusing unexpected asset name: {asset.name!r}")
    name = safe_filename(Path(asset.url.split("?", 1)[0]).name or asset.name)
    if not name or name in {".", ".."} or len(name) > 100:
        raise ValueError(f"Refusing unsafe asset name: {asset.name!r}")
    if not _ASSET_NAME_RE.match(name):
        raise ValueError(f"Refusing unexpected asset file name: {name!r}")
    if name not in {f"telegram-downloader_{tag_version}_amd64.deb",
                    f"telegram-downloader_{tag_version}.dmg",
                    f"telegram-downloader_{tag_version}.apk"}:
        raise ValueError(
            f"Asset {name!r} does not belong to release v{tag_version}.")
    checksum_name = _checksum_name_for(name)
    if checksum_name is None:
        raise ValueError(f"No checksum file defined for asset: {name!r}")

    # ---- U1/U2: verification happens FIRST, on the network-free side of the
    # ---- download. Nothing is written to disk until the manifest's signature
    # ---- and the expected digest are both known-good.
    sums = _fetch_verified_sums(checksum_name, tag_version, timeout=timeout,
                                token=token, allow_unsigned=allow_unsigned)
    digest = sums.get(name)
    if digest is None:
        raise ValueError(f"Asset {name!r} missing from {checksum_name}; refusing unverified installer.")

    dest_dir = Path(dest_dir).expanduser()
    try:
        resolved_dd = dest_dir.resolve()
    except OSError:
        resolved_dd = dest_dir.absolute()
    resolved_dd.mkdir(parents=True, exist_ok=True)
    # Sweep leftovers of runs that were killed before they could clean up.
    # Done BEFORE we mint our own tmp name so we can never race ourselves.
    _sweep_stale_parts(resolved_dd)
    dest = resolved_dd / name
    # Random-suffix tmp + O_NOFOLLOW|O_EXCL: no predictable-name symlink plant.
    nofollow = getattr(_os, "O_NOFOLLOW", 0)
    tmp = resolved_dd / f"{name}.part.{_os.getpid()}.{_secrets.token_hex(8)}"
    try:
        if tmp.is_symlink():
            raise ValueError("Refusing to write through symlink tmp file.")
    except OSError:
        pass
    # No Authorization header here on purpose: _urlopen_no_redirect() owns
    # token policy (send nothing unless GitHub answers 403/429).
    req = urllib.request.Request(asset.url, headers={
        "User-Agent": "telegram-downloader",
    })
    total = 0
    import time as _time
    deadline = _time.monotonic() + 3600  # hard stop for a slow/hung CDN stream
    # One try/finally around EVERYTHING from the first byte to the last
    # chmod. The promise in the comment below ("a rejected installer must not
    # sit in the destination dir") used to be kept by enumerating the
    # exceptions we expected — so an OSError out of _verify_file_sha256, an
    # OSError out of tmp.replace(), or a KeyboardInterrupt/cancellation during
    # hashing each left the whole payload behind as
    # `<installer>.part.<pid>.<hex>`. A finally cannot be forgotten by the
    # next failure mode that gets added, so that is what carries the cleanup.
    fd = -1
    renamed = False     # tmp has been moved onto dest
    published = False   # dest is the verified installer; leave it alone
    try:
        fd = _os.open(tmp, _os.O_WRONLY | _os.O_CREAT | _os.O_EXCL | nofollow, 0o600)
        with _urlopen_no_redirect(req, timeout=60, token=token or None) as resp:
            declared = resp.headers.get("Content-Length")
            if declared is not None:
                try:
                    declared_size: int | None = int(declared)
                except (TypeError, ValueError):
                    declared_size = None
                if declared_size is not None and declared_size > MAX_ASSET_BYTES:
                    raise ValueError(f"Asset too large ({declared_size} bytes).")
            with _os.fdopen(fd, "wb") as fh:
                fd = -1
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_ASSET_BYTES:
                        raise ValueError("Asset exceeded the size cap during download.")
                    if _time.monotonic() > deadline:
                        raise ValueError("Asset download exceeded the time budget; aborted.")
                    fh.write(chunk)
        try:
            if tmp.is_symlink():
                raise ValueError("Refusing to publish through symlink tmp file.")
        except OSError:
            # Cannot even stat it, so we cannot prove it is not a symlink:
            # fail closed (the finally below removes whatever is there).
            raise
        _harden(tmp)
        _verify_file_sha256(tmp, digest)
        tmp.replace(dest)
        renamed = True
        # Re-verify the PUBLISHED file through O_NOFOLLOW: closes the window
        # where tmp was swapped after the checksum pass (a planted symlink or
        # replaced inode right before rename would otherwise present itself as
        # the trusted installer).
        _verify_file_sha256(dest, digest)
        try:
            # 0o600 -- owner read+write, nothing for group/other.
            #
            # This used to read `0o600 & ~0x111`, intending to "remove the
            # write bits", but 0x111 IS the three user-write bits, so the
            # mask evaluates to 0o400: WRITE-ONLY, with the read bit
            # stripped. Every reader then failed, including the app's own
            # test suite and `apt-get install` on the downloaded .deb.
            # 0600 is the correct and intended mode for a private file.
            dest.chmod(0o600)
        except OSError:
            pass
        published = True
        return dest
    finally:
        if fd != -1:
            try:
                _os.close(fd)
            except OSError:
                pass
        if not published:
            # Leave nothing behind: a rejected installer must not sit in the
            # destination dir where the user (or a later "open folder" step)
            # could pick it up — on every failure mode, not just the
            # ValueErrors we thought of when this was written.
            _unlink_quietly(tmp)
            # Only ours: `renamed` proves dest is this run's file and not a
            # good installer left by an earlier successful download.
            if renamed:
                _unlink_quietly(dest)


# --------------------------------------------------------------------------
# Local install
# --------------------------------------------------------------------------
# SECURITY NOTE, stated plainly rather than buried: this installs a file that
# was fetched over the network, as root, via pkexec. The caller is expected
# to have ALREADY verified the download (download_asset checks the digest
# against the release manifest); what this function adds is no verification
# of its own. pkexec shows the user's own password prompt, so the human — not
# this code — is the consent boundary. It is deliberately a separate function
# from download_asset so that "download" and "run as root" stay two visible,
# separately-callable steps rather than one fused operation.
INSTALL_TIMEOUT_SECONDS = 300


def install_deb(path) -> tuple[bool, str]:
    """Install a downloaded .deb with apt-get under pkexec.

    Returns (ok, message). Runs synchronously — call it via
    asyncio.to_thread so the UI thread is not blocked while the user types
    their password.

    `pkexec` is used rather than sudo because it goes through polkit, which
    shows the distribution's own authentication dialog. That prompt is the
    security boundary here: nothing is installed without an explicit human
    action, and the password never passes through this process.

    argv is a list, never a shell string, so a filename can never be
    interpreted as shell syntax.
    """
    path = os.fspath(path)
    if not os.path.isfile(path):
        return False, f"Installer not found: {path}"
    if shutil.which("pkexec") is None:
        return False, ("pkexec is not available on this system, so the "
                       "installer cannot be run with your password. Install it "
                       "manually.")
    try:
        result = subprocess.run(
            ["pkexec", "apt-get", "install", "-y", os.path.abspath(path)],
            capture_output=True, text=True, timeout=INSTALL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False, "Installation timed out."
    except FileNotFoundError:
        return False, "pkexec is not available on this system."
    except OSError as exc:
        return False, str(exc)
    if result.returncode == 0:
        return True, "Update installed successfully."
    err = (result.stderr or result.stdout or "Unknown error").strip()
    return False, err[-500:] or f"apt-get exited {result.returncode}"


def installable_asset(info):
    """The asset we can actually install in place, or None.

    Only .deb can be installed unattended on a desktop. A .dmg needs a
    mounted disk image and a drag-to-Applications step; an .apk is an Android
    package that a desktop Linux machine cannot install at all. Rather than
    pretend, those fall back to opening the release page.
    """
    asset = info.asset_for_platform() if info is not None else None
    if asset is None:
        return None
    if getattr(asset, "name", "").endswith(".deb"):
        return asset
    return None

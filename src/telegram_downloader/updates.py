"""In-app update checker.

Mirrors simple-yt-downloader's update_checker.dart: query the GitHub
releases API, compare versions, and pick the right installer asset for
the current platform. Stdlib only (urllib) so the CLI stays dependency-free.

Release-asset naming convention (see scripts/build-*.sh + CI):
    telegram-downloader_<ver>_amd64.deb   Linux
    telegram-downloader_<ver>.dmg         macOS
    telegram-downloader_<ver>.apk         Android
"""
from __future__ import annotations

import json
import platform
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

REPO = "MDHasan0078/telegram-downloader"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"

# Where installer/release URLs may point. Anything else (file://, intranet,
# lookalike hosts) is refused before opening or downloading.
RELEASE_HOSTS = {"github.com"}
ASSET_HOSTS = {"github.com", "objects.githubusercontent.com",
               "release-assets.githubusercontent.com"}
MAX_ASSET_BYTES = 800 * 1024 ** 2


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
            if operating_system == "linux" and "com.termux" in __import__("os").environ.get("PREFIX", ""):
                operating_system = "android"
        if operating_system == "windows":
            versioned = {f"telegram-downloader_{self.latest_version}_win64.exe"}
        elif operating_system in ("darwin", "macos"):
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
    """Allowlist check: https only, exact host match (no userinfo/ports tricks)."""
    try:
        parts = urllib.parse.urlparse(url)
        return (parts.scheme == "https" and parts.hostname in hosts
                and not parts.username and not parts.password
                and parts.port in (None, 443))
    except Exception:
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse automatic redirects so the allowlist can't be bypassed by a
    302 from an allowlisted host to an attacker URL (SSRF/downgrade)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _urlopen_no_redirect(req: urllib.request.Request, timeout: int):
    # Disable env proxies (HTTPS_PROXY gag) — otherwise local attacker can
    # MITM update checks via env var. Explicit empty ProxyHandler.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    return opener.open(req, timeout=timeout)


_VERSION_RE = re.compile(r"^v?\d+\.\d+\.\d+\Z")
# Bound to our repo: any other owner/repo must not open as a trusted update.
# Use \Z not $ to block trailing \n injection.
_RELEASE_URL_RE = re.compile(
    rf"^https://github\.com/{re.escape(REPO)}/releases/tag/v?\d+\.\d+\.\d+\Z")
# Version-bound, case-sensitive, no traversal: only our exact release
# filenames are ever downloaded. \Z blocks trailing newline bypass.
_ASSET_NAME_RE = re.compile(
    r"^telegram-downloader_\d+\.\d+\.\d+(_amd64|_win64)?\.(deb|dmg|apk|exe)\Z")


def check_for_update(repo: str = REPO, timeout: int = 10) -> UpdateInfo | None:
    """Return UpdateInfo or None (offline / no releases / rate-limited). Never raises."""
    if repo != REPO:
        # The updater UI presents results as trusted: never allow callers to
        # point it at an arbitrary repo (attacker release = trusted prompt).
        return None
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/releases/latest",
            headers={"User-Agent": "telegram-downloader",
                     "Accept": "application/vnd.github+json"},
        )
        with _urlopen_no_redirect(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode("utf-8"))
        assets = []
        for a in (data.get("assets") or []):
            raw_url = a.get("browser_download_url") or ""
            raw_name = a.get("name") or ""
            # Ignore assets whose browser_download_url isn't on our
            # host allowlist or whose name isn't version-bound — a
            # compromised release can't smuggle a cross-repo payload.
            if not _https_url_ok(raw_url, ASSET_HOSTS):
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
        expected = {
            f"telegram-downloader_{tag_ver}_amd64.deb",
            f"telegram-downloader_{tag_ver}.dmg",
            f"telegram-downloader_{tag_ver}.apk",
            f"telegram-downloader_{tag_ver}_win64.exe",
        }
        assets = [a for a in assets if a.name in expected]
        return UpdateInfo(latest_version=tag.lstrip("v").lstrip("V"),
                          release_url=release_url,
                          assets=assets)
    except Exception:
        return None


def open_release_page(url: str) -> None:
    if not _https_url_ok(url, RELEASE_HOSTS) or not _RELEASE_URL_RE.match(url):
        raise ValueError(f"Refusing to open untrusted URL: {url!r}")
    if sys.platform.startswith("linux"):
        subprocess.Popen(["xdg-open", url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    elif sys.platform == "win32":
        # NOTE: list-form Popen does NOT protect cmd.exe metachars, so the
        # strict allowlist above is load-bearing here. webbrowser avoids cmd.
        import webbrowser
        webbrowser.open(url)
    else:
        print(url)


def download_asset(asset: UpdateAsset, dest_dir) -> object:
    """Download an installer asset into *dest_dir*. Returns the final path.

    Hardened: https + asset-host allowlist, no-redirect fetch, strict
    release filename, sanitized name, size cap, symlink-safe atomic write.
    Never executes the result.
    """
    import os as _os
    import secrets as _secrets
    from pathlib import Path
    from .config import harden_private_file as _harden
    from .telegram_utils import safe_filename
    if not _https_url_ok(asset.url, ASSET_HOSTS):
        raise ValueError(f"Refusing to download from untrusted URL: {asset.url!r}")
    if not _ASSET_NAME_RE.match(asset.name or ""):
        raise ValueError(f"Refusing unexpected asset name: {asset.name!r}")
    name = safe_filename(Path(asset.url.split("?", 1)[0]).name or asset.name)
    if not name or name in {".", ".."} or len(name) > 100:
        raise ValueError(f"Refusing unsafe asset name: {asset.name!r}")
    if not _ASSET_NAME_RE.match(name):
        raise ValueError(f"Refusing unexpected asset file name: {name!r}")
    dest_dir = Path(dest_dir).expanduser()
    try:
        resolved_dd = dest_dir.resolve()
    except OSError:
        resolved_dd = dest_dir.absolute()
    resolved_dd.mkdir(parents=True, exist_ok=True)
    dest = resolved_dd / name
    # Random-suffix tmp + O_NOFOLLOW|O_EXCL: no predictable-name symlink plant.
    nofollow = getattr(_os, "O_NOFOLLOW", 0)
    tmp = resolved_dd / f"{name}.part.{_os.getpid()}.{_secrets.token_hex(8)}"
    try:
        if tmp.is_symlink():
            raise ValueError("Refusing to write through symlink tmp file.")
    except OSError:
        pass
    req = urllib.request.Request(asset.url, headers={"User-Agent": "telegram-downloader"})
    total = 0
    fd = _os.open(tmp, _os.O_WRONLY | _os.O_CREAT | _os.O_EXCL | nofollow, 0o600)
    try:
        with _urlopen_no_redirect(req, timeout=60) as resp:
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
                    fh.write(chunk)
    except BaseException:
        if fd != -1:
            try:
                _os.close(fd)
            except OSError:
                pass
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    try:
        if tmp.is_symlink():
            tmp.unlink()
            raise ValueError("Refusing to publish through symlink tmp file.")
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    _harden(tmp)
    tmp.replace(dest)
    try:
        dest.chmod(0o600 & ~0o111)
    except OSError:
        pass
    return dest

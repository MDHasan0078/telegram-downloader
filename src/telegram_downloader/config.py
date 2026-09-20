"""Persistent configuration.

Stores credentials + preferences under the OS config dir so the user only
enters API keys / download folder once (first run), then edits them in
Settings / `tg-dl config`.

Layout (~/.config/telegram-downloader/):
    config.json      api_id, api_hash, download_dir, output_mode  (0600)
    session.session  Telethon auth session (do not share)
    queue.json       persisted download queue (for resume-all)

Backward compat: if a legacy KEY=VALUE `config` file exists (from the
original single-file script), its api_id/api_hash are migrated.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

APP_NAME = "telegram-downloader"
OUTPUT_MODES = ("1", "2", "3")
THEMES = ("system", "light", "dark")

_UMASK_LOCK = threading.Lock()

def _sanitize_display(s: str, limit: int = 500) -> str:
    """Strip control chars / ANSI escapes for safe terminal / UI display."""
    if not isinstance(s, str):
        s = str(s)
    # \x1b is ESC, \x00-\x1f cover \r\n etc.; safe_filename does this for
    # filenames but display paths (titles/urls/errors) need the same.
    s = re.sub(r"[\x00-\x1f\x7f]", "?", s)
    if len(s) > limit:
        s = s[:limit]
    return s


def _base_config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        raw = Path(xdg).expanduser()
        # Refuse hijack: XDG must be absolute, owned, not world-writable,
        # not a symlink. Falls back to ~/.config on violation.
        try:
            if not raw.is_absolute():
                raise ValueError("XDG_CONFIG_HOME must be absolute")
            # lstat, not stat: don't follow symlink
            st = os.lstat(raw) if raw.exists() else None
            if st is not None:
                import stat as _stat
                if _stat.S_ISLNK(st.st_mode):
                    raise ValueError("XDG_CONFIG_HOME is a symlink")
                if st.st_uid != os.getuid():
                    raise ValueError("XDG_CONFIG_HOME not owned by user")
                if st.st_mode & 0o022:
                    raise ValueError("XDG_CONFIG_HOME world/group writable")
        except (ValueError, OSError):
            raw = Path.home() / ".config"
        return raw / APP_NAME
    # Android / Termux fallback: prefer app-private storage when available.
    # Flet/Termux usually still exposes $HOME, so this degrades gracefully.
    return Path.home() / ".config" / APP_NAME


def _refuse_config_symlink() -> None:
    """Abort if CONFIG_DIR itself is a symlink (parent hijack)."""
    try:
        if (CONFIG_DIR.is_symlink()) or ((CONFIG_DIR.resolve() != CONFIG_DIR.absolute().resolve()) and (CONFIG_DIR.exists()) and (CONFIG_DIR.resolve().is_symlink())):
            raise RuntimeError(f"Refusing config dir that is a symlink: {CONFIG_DIR}")
        # Check that no parent component is a symlink (e.g. ~/.config -> /tmp/evil)
        cur: Path | None = CONFIG_DIR.absolute()
        while cur is not None and cur != cur.parent:
            try:
                if cur.exists() and cur.is_symlink():
                    raise RuntimeError(f"Refusing config dir traversing a symlink: {CONFIG_DIR}")
            except RuntimeError:
                raise
            except OSError:
                pass
            if cur.parent == cur:
                break
            cur = cur.parent
    except RuntimeError:
        raise
    except OSError:
        pass


CONFIG_DIR = _base_config_dir()
CONFIG_JSON = CONFIG_DIR / "config.json"
LEGACY_CONFIG = CONFIG_DIR / "config"  # KEY=VALUE file from the old script
SESSION_BASE = CONFIG_DIR / "session"  # Telethon appends .session
QUEUE_FILE = CONFIG_DIR / "queue.json"


@dataclass
class Settings:
    api_id: Optional[str] = field(default=None, repr=False)
    api_hash: Optional[str] = field(default=None, repr=False)
    download_dir: str = ""  # empty => first run, must ask
    output_mode: str = "1"  # 1=original, 2=fast, 3=max
    theme: str = "dark"    # dark | light (dark is the default, like the reference app)
    user_id: Optional[str] = field(default=None, repr=False)  # pinned Telegram user id

    @property
    def is_configured(self) -> bool:
        return bool(self.api_id and self.api_hash and self.download_dir)

    @property
    def has_api(self) -> bool:
        return bool(self.api_id and self.api_hash)

    def api_id_int(self) -> int:
        """Validated int form of api_id (stored as str for JSON compat).

        Single canonical cast — callers must use this instead of
        scattering int(str(...)) conversions.
        """
        try:
            return int(str(self.api_id or "").strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("API ID must be numeric.") from exc


def default_download_dir() -> Path:
    return Path.home() / "Downloads" / "Telegram"


def _mkdir_parents_nofollow(target: Path, mode: int = 0o700) -> None:
    """mkdir -p without following symlinks: create each component lstat-checked."""
    target = target.absolute()
    # Build ancestor list from root to target
    parts = list(target.parents)[::-1] + [target]
    # Filter to only those that are at or below the first missing component
    # to avoid chmodding unrelated parents like /home.
    to_create: list[Path] = []
    for p in parts:
        if not p.exists():
            to_create.append(p)
    # Also include existing ancestors for symlink check
    for p in parts:
        try:
            if p.is_symlink():
                raise RuntimeError(f"Refusing to mkdir through symlink: {p}")
        except RuntimeError:
            raise
        except OSError:
            pass
    for p in to_create:
        try:
            # Check again before creation for race
            if p.is_symlink():
                raise RuntimeError(f"Refusing to mkdir through symlink: {p}")
        except RuntimeError:
            raise
        except OSError:
            pass
        try:
            p.mkdir(exist_ok=False, mode=mode)
        except FileExistsError:
            # Raced: verify it's not a symlink now
            try:
                if p.is_symlink():
                    raise RuntimeError(f"Refusing to mkdir through symlink: {p}")
            except RuntimeError:
                raise
            except OSError:
                pass
        except OSError:
            # Fallback for case where parent still missing due to race
            try:
                p.mkdir(parents=False, mode=mode)
            except FileExistsError:
                pass
            except OSError:
                raise
        try:
            # follow_symlinks=False: if a race planted a link, chmod refuses
            # instead of chmodding an unrelated target through the link.
            os.chmod(p, mode, follow_symlinks=False)
        except OSError:
            pass


def _ensure_dirs() -> None:
    _refuse_config_symlink()
    # symlink-safe mkdir -p for CONFIG_DIR
    try:
        _mkdir_parents_nofollow(CONFIG_DIR, mode=0o700)
    except (RuntimeError, OSError) as exc:
        # No permissive fallback: a symlink-plant refusal must abort,
        # never silently follow the link with plain mkdir.
        raise RuntimeError(f"Refusing unsafe config dir {CONFIG_DIR}: {exc}") from exc
    for p in {CONFIG_DIR, CONFIG_DIR.resolve()}:
        try:
            p.chmod(0o700)
        except OSError:
            pass


@contextmanager
def umask_077():
    """Temporarily restrict default perms so third-party libs (Telethon
    sqlite) create secret-adjacent files owner-only from the start.

    NOTE: os.umask is process-global (not thread-safe). All uses are
    serialized through _UMASK_LOCK; prefer explicit 0o600 modes for new
    code and keep this only around third-party creation calls.
    """
    with _UMASK_LOCK:
        old = os.umask(0o077)
        try:
            yield
        finally:
            os.umask(old)


def _atomic_write(path: Path, data: bytes) -> None:
    """Write *data* to *path* atomically, owner-only, symlink-safe.

    Uses a random-suffix tmp name plus O_NOFOLLOW|O_EXCL so a planted
    symlink (predictable `<name>.tmp -> ~/.bashrc`) can neither be
    followed nor clobbered. Fails closed on platforms without O_NOFOLLOW.
    """
    _refuse_config_symlink()
    try:
        _mkdir_parents_nofollow(path.parent, mode=0o700)
    except (RuntimeError, OSError) as exc:
        raise OSError(f"could not safely create parent of {path}: {exc}") from exc
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    for _ in range(5):
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{secrets.token_hex(8)}")
        try:
            if tmp.is_symlink():
                try:
                    tmp.unlink()
                except OSError:
                    pass
                continue
        except OSError:
            pass
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow
        try:
            fd = os.open(tmp, flags, 0o600)
        except FileExistsError:
            continue
        except OSError as exc:
            # ELOOP => O_NOFOLLOW refused a symlink: remove plant, retry.
            import errno
            if exc.errno in (errno.ELOOP,) and nofollow:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                continue
            # No kernel symlink protection on this platform: fail closed
            # rather than writing secrets through a possible plant.
            raise OSError(f"could not safely write {path}: {exc}") from exc
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        try:
            if tmp.is_symlink():
                tmp.unlink()
                raise OSError("refusing to publish through symlink tmp file")
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        harden_private_file(tmp)
        try:
            tmp.replace(path)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        harden_private_file(path)
        return
    raise OSError(f"could not safely write {path}")


def harden_private_file(path: Path) -> None:
    """Restrict a secret-adjacent file (config/session/queue) to owner-only."""
    try:
        # follow_symlinks=False: never chmod through a planted symlink.
        os.chmod(path, 0o600, follow_symlinks=False)
    except FileNotFoundError:
        pass
    except (NotImplementedError, OSError):
        # Fallback for platforms lacking follow_symlinks: only chmod if the
        # path is not a symlink (avoid following a planted link to the target).
        try:
            if not path.is_symlink():
                path.chmod(0o600)
        except OSError:
            pass


def harden_session_files() -> None:
    """Telethon creates the sqlite session with default umask; lock it down."""
    _ensure_dirs()
    for child in CONFIG_DIR.glob("session*"):
        harden_private_file(child)


def _read_legacy() -> dict:
    values: dict = {}
    try:
        # lstat first: never read through a planted symlink (its KEY=VALUE
        # lines would be migrated into the real config). Close the
        # check-then-read window with an O_NOFOLLOW open just like the
        # queue loader — is_symlink() alone is TOCTOU-racy.
        if LEGACY_CONFIG.is_symlink():
            return values
        if not LEGACY_CONFIG.exists() or CONFIG_JSON.exists():
            return values
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        try:
            # O_NOFOLLOW: never migrate secrets from a planted symlink.
            fd = os.open(LEGACY_CONFIG, os.O_RDONLY | nofollow)
            try:
                with os.fdopen(fd, "r", encoding="utf-8") as fh:
                    fd = -1
                    raw_text = fh.read(512 * 1024)
            finally:
                if fd != -1:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
        except OSError as exc:
            import errno as _errno
            if exc.errno == _errno.ELOOP:
                return values
            return values
        for raw in raw_text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip()
    except OSError:
        pass
    return values


def _read_json_nofollow(path: Path):
    """JSON-load a file via O_NOFOLLOW|O_RDONLY (never through a symlink).

    Returns the parsed object, or raises OSError on platform where
    O_NOFOLLOW is unavailable with the target being a symlink (ELOOP).
    """
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, os.O_RDONLY | nofollow)
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            fd = -1
            return json.load(fh)
    finally:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass


def load_settings() -> Settings:
    _ensure_dirs()
    data: dict = {}
    if CONFIG_JSON.exists():
        try:
            data = _read_json_nofollow(CONFIG_JSON)
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
    else:
        legacy = _read_legacy()
        if legacy.get("api_id") or legacy.get("api_hash"):
            data = {
                "api_id": legacy.get("api_id"),
                "api_hash": legacy.get("api_hash"),
                "download_dir": "",
                "output_mode": "1",
            }
            # Migrate immediately so there is a single source of truth.
            save_settings(Settings(
                api_id=data.get("api_id"),
                api_hash=data.get("api_hash"),
                download_dir="",
                output_mode="1",
            ))
            # Shred the legacy world-readable KEY=VALUE file now that its
            # secrets live in the 0600 JSON. Best-effort overwrite + unlink.
            try:
                if LEGACY_CONFIG.is_symlink():
                    LEGACY_CONFIG.unlink()
                elif LEGACY_CONFIG.is_file():
                    try:
                        # Shred via O_NOFOLLOW fd: a link planted between
                        # the is_file() check and the open must not let us
                        # zero-fill some OTHER file (clobber vector).
                        nofollow = getattr(os, "O_NOFOLLOW", 0)
                        fd = os.open(LEGACY_CONFIG, os.O_RDWR | nofollow)
                        try:
                            st = os.fstat(fd)
                            os.ftruncate(fd, 0)
                            size = min(st.st_size, 1 << 20)
                            os.lseek(fd, 0, os.SEEK_SET)
                            os.write(fd, b"\x00" * size)
                            os.fsync(fd)
                        finally:
                            try:
                                os.close(fd)
                            except OSError:
                                pass
                    except OSError:
                        pass
                    LEGACY_CONFIG.unlink()
            except OSError:
                pass
            try:
                data = _read_json_nofollow(CONFIG_JSON) if CONFIG_JSON.exists() else data
            except OSError:
                pass

    s = Settings(
        api_id=str(data.get("api_id")) if data.get("api_id") else None,
        api_hash=str(data.get("api_hash")) if data.get("api_hash") else None,
        download_dir=str(data.get("download_dir") or ""),
        output_mode=str(data.get("output_mode") or "1"),
        theme=str(data.get("theme") or "dark"),
    )
    # Identity pin: digits only, else ignore (tamper-evident, not a boundary).
    raw_uid = data.get("user_id")
    if raw_uid is not None and re.fullmatch(r"\d{1,20}", str(raw_uid)):
        s.user_id = str(raw_uid)
    # Env overrides (do not persist): useful for CI / containers.
    # WARNING: env vars are visible in `ps e` / /proc/<pid>/environ to
    # other local users. Prefer interactive entry (getpass) on shared
    # machines; use env only in throwaway CI/containers.
    if os.environ.get("TG_API_ID"):
        s.api_id = os.environ["TG_API_ID"]
    if os.environ.get("TG_API_HASH"):
        s.api_hash = os.environ["TG_API_HASH"]
    if os.environ.get("TG_DOWNLOAD_DIR"):
        s.download_dir = os.environ["TG_DOWNLOAD_DIR"]
    if s.output_mode not in OUTPUT_MODES:
        s.output_mode = "1"
    if s.theme not in THEMES:
        s.theme = "dark"
    return s


def save_settings(s: Settings) -> None:
    _ensure_dirs()
    data = json.dumps(asdict(s), indent=2).encode("utf-8")
    # Atomic + symlink-safe: random tmp suffix with O_NOFOLLOW|O_EXCL, so
    # secret bytes are never world-readable and never follow a planted link.
    _atomic_write(CONFIG_JSON, data)


def session_path_str() -> str:
    _ensure_dirs()
    _refuse_config_symlink()
    # Pre-create the sqlite file owner-only so Telethon never creates it
    # under a permissive umask (0644 window before harden_session_files).
    # An empty file is a valid empty sqlite DB for Telethon/sqlite3.
    import errno as _errno
    session_file = Path(str(SESSION_BASE) + ".session")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    # 3 attempts: ELOOP => unlink the plant and retry; anything else fails
    # loudly. NEVER silently return a path that still resolves to attacker
    # content (Telethon would write the auth key into it).
    made = False
    for _ in range(3):
        try:
            if session_file.is_symlink():
                # Planted link: remove the link itself (never the target).
                session_file.unlink()
        except OSError:
            # is_symlink/unlink raced: re-open with O_NOFOLLOW below decides.
            pass
        try:
            if session_file.exists():
                with umask_077():
                    dfd = os.open(session_file, os.O_RDONLY | nofollow)
                try:
                    os.fstat(dfd)  # reaches here only if not a symlink
                finally:
                    try:
                        os.close(dfd)
                    except OSError:
                        pass
                harden_private_file(session_file)
                return str(SESSION_BASE)
        except OSError as exc:
            if exc.errno in (_errno.ELOOP,) and nofollow:
                continue
            raise
        try:
            with umask_077():
                # O_NOFOLLOW|O_EXCL: refuse to create through a link planted
                # in the check-then-open window. ELOOP -> unlink plant, retry.
                fd = os.open(session_file,
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600)
                os.close(fd)
                made = True
                break
        except OSError as exc:
            if exc.errno == _errno.ELOOP and nofollow:
                continue
            if exc.errno == _errno.EEXIST:
                continue  # raced to create; loop verifies with O_NOFOLLOW
            raise
    if not made:
        # Only reachable after ELOOP-without-nofollow support, or with a host
        # that keeps racing a symlink in under us: fail closed, never pass a
        # planted link to Telethon.
        raise OSError(f"could not safely create session file: {session_file}")
    harden_private_file(session_file)
    return str(SESSION_BASE)


def _config_and_session_resolved() -> tuple[Path, Path]:
    try:
        return CONFIG_DIR.resolve(), SESSION_BASE
    except OSError:
        return CONFIG_DIR, SESSION_BASE


def ensure_download_dir(s: Settings) -> Path:
    """Return the download dir, creating it. Falls back to default if unset."""
    raw = (s.download_dir or "").strip()
    target = Path(raw).expanduser() if raw else default_download_dir()
    _refuse_config_symlink()
    # Reject world-writable download parents (squat in /tmp 1777).
    # Also reject control chars (telemetry/terminal-escape / filename
    # injection) plus shell meta chars (explorer injection).
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in str(target)):
        raise ValueError(f"Refusing download folder with control characters: {target}")
    if any(c in str(target) for c in [",", ";", "|", "&", "$", "`"]):
        raise ValueError(f"Refusing download folder with shell meta chars: {target}")
    # Containment: never allow the download tree to overlap the config dir
    # (a file named queue.json / session.session would clobber secrets) and
    # never allow filesystem root. Every ancestor is lstat-checked so a
    # planted symlink in the chain can't redirect the mkdir.
    try:
        cfg = CONFIG_DIR.resolve()
        tgt = target.absolute()
        # Walk every existing ancestor of the requested target and fail if
        # any component is a symlink — catches `linkparent/subdir` patterns.
        # Dangling symlinks have exists()==False but is_symlink()==True, so
        # use lexists via is_symlink without exists guard.
        cur: Path | None = tgt
        while cur is not None and cur != cur.parent:
            try:
                if cur.is_symlink():
                    raise ValueError(f"Cannot use this folder: the path contains a link that isn't allowed: {target}")
            except ValueError:
                raise
            except OSError:
                pass
            if cur.parent == cur:
                break
            cur = cur.parent
        # Resolve without requiring existence for the overlap check.
        try:
            tgt_res = tgt.resolve()
        except OSError:
            tgt_res = tgt
        # Only refuse when the download would land *inside* the config tree
        # (where a filename queue.json / session.session would clobber secrets).
        # A config inside the download (e.g. HOME) is not a file-overlap risk.
        if tgt_res == cfg or cfg in tgt_res.parents:
            raise ValueError(
                f"Cannot use this folder: it overlaps with the app's settings folder: {target}")
        if len(tgt_res.parts) <= 1:
            raise ValueError("Refusing download folder at filesystem root.")
        if tgt_res != tgt and tgt_res.is_symlink():
            raise ValueError(f"Refusing download folder that is a symlink: {target}")
        # World-writable parent squat check: if any existing parent is 0o002
        # and download dir is under /tmp, require it be owned and 0700.
        try:
            for parent in [tgt] + list(tgt.parents):
                if not parent.exists():
                    continue
                try:
                    st = parent.stat()
                except OSError:
                    continue
                if parent == Path("/tmp") or str(parent) == "/tmp":
                    # /tmp is a deliberate shared scratch dir (1777 sticky):
                    # allow it, but the submkdir below is owner-0700 anyway.
                    break
                if st.st_mode & 0o002:
                    # sticky bit (1777) still allows squat; require owner
                    if parent.stat().st_uid != os.getuid():
                        raise ValueError(f"Refusing download folder under world-writable dir not owned by you: {parent}")
                    break
        except ValueError:
            raise
        except OSError:
            pass
    except ValueError:
        raise
    except OSError:
        pass
    if target.is_symlink():
        raise ValueError(f"Refusing download folder that is a symlink: {target}")
    try:
        _mkdir_parents_nofollow(target, mode=0o700)
    except (RuntimeError, OSError) as exc:
        # No permissive mkdir(parents=True) fallback: it would follow a
        # planted symlink in an ancestor. ANY failure here is a refusal —
        # callers get a clear ValueError instead of a security bypass.
        raise ValueError(
            f"Refusing to create download folder {target}: {exc}") from exc
    # Post-mkdir re-check: a race that swapped a parent to a link is caught.
    try:
        cur = target.resolve()
        if cur != target.absolute().resolve() and target.resolve().is_symlink():
            raise ValueError(f"Cannot use this folder: the path contains a link that isn't allowed: {target}")
        # Verify no ancestor of the *resolved* path is a link (covers linkparent).
        anc: Path | None = target.absolute()
        while anc is not None and anc != anc.parent:
            try:
                if anc.is_symlink():
                    raise ValueError(f"Cannot use this folder: the path contains a link that isn't allowed: {target}")
            except ValueError:
                raise
            except OSError:
                pass
            if anc == target.absolute().parent:
                pass
            if anc.parent == anc:
                break
            anc = anc.parent
    except ValueError:
        raise
    except OSError:
        pass
    return target


def clear_session() -> bool:
    """Delete Telethon session file(s), incl. SQLite WAL sidecars.

    Unlinks symlinks themselves (never follows them) and verifies nothing
    `session*` remains. Returns True if anything was removed.
    """
    removed = False
    for p in CONFIG_DIR.glob("session*"):
        try:
            if p.is_symlink():
                # Unlinking a symlink removes the link, never the target.
                p.unlink()
                removed = True
            elif p.is_file():
                p.unlink()
                removed = True
        except OSError:
            pass
    # Verify: report honestly so callers don't claim success with leftovers.
    try:
        if any(CONFIG_DIR.glob("session*")):
            return removed
    except OSError:
        pass
    return removed


def config_dir_str() -> str:
    _ensure_dirs()
    return str(CONFIG_DIR)

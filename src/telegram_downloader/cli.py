"""CLI: `tg-dl gui|download|batch|preview|config|deps`."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

from . import __version__
from . import converter, deps as deps_mod
from . import tg_client
from .config import (_sanitize_display, clear_session, config_dir_str,
                     default_download_dir, ensure_download_dir,
                     harden_session_files, load_settings, save_settings)
from .constants import MAX_BATCH_BYTES, MAX_URLS
from .converter import ffmpeg_to_mp4
from .downloader import Cancelled, DownloadError, download_resumable
from .queue_store import DownloadItem, QueueStore
from .telegram_utils import (auto_title_from_message, format_bytes,
                             infer_extension, parse_message_url, safe_filename,
                             split_urls)


# ---------------------------------------------------------------- utils

def _die(msg: str, code: int = 1):
    print(f"\nError: {msg}", file=sys.stderr)
    raise SystemExit(code)


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    v = input(f"{prompt}{suffix}: ").strip()
    return v or default


def _mask_id(value: str) -> str:
    # Fixed mask: a length-proportional mask would disclose the ID length.
    return "***" if str(value or "") else "(not set)"


def _redact_home(path: str) -> str:
    try:
        home = str(Path.home())
        if path == home:
            return "~"
        if path.startswith(home + "/"):
            return "~" + path[len(home):]
    except Exception:
        pass
    return path


def _ensure_api(s):
    if s.api_id and s.api_hash:
        print(f"Using saved API ID: {_mask_id(s.api_id)}")
        return s
    print("First-time setup: Telegram API credentials are required.")
    print("Create an app at https://my.telegram.org -> API development tools.")
    api_id = _ask("Enter Telegram API ID", s.api_id or "")
    try:
        # getpass: the hash must not echo to the terminal / scrollback.
        api_hash = getpass.getpass("Enter Telegram API hash: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        _die("API hash entry cancelled.")
    if not api_id or not api_hash:
        _die("API ID and hash are required (Settings → Connection).")
    try:
        int(api_id)
    except ValueError:
        _die("API ID must be a number.")
    s.api_id, s.api_hash = api_id.strip(), api_hash.strip()
    save_settings(s)
    return s


def _ensure_dl_dir(s):
    # No exists() fast-path: every saved value goes through containment /
    # symlink / root validation, and ValueError becomes a clean error.
    if not s.download_dir:
        print("\nFirst-time setup: where should files be saved?")
        print(f"Default: {default_download_dir()}")
        chosen = _ask("Download folder (Enter for default)", str(default_download_dir()))
        s.download_dir = chosen.strip() or str(default_download_dir())
        save_settings(s)
        print(f"Saved. Change anytime in Settings or: tg-dl config set download_dir=<path>")
    try:
        ensure_download_dir(s)
    except (OSError, ValueError) as exc:
        _die(f"Cannot use download folder: {exc}")
    return s


class _CliProgress:
    def __init__(self, total: int, label: str):
        import time
        self.total = total
        self.label = label
        self._t0 = time.monotonic()

    def __call__(self, cur: int, total: int):
        import time
        self.total = total or self.total
        el = max(time.monotonic() - self._t0, 0.001)
        sp = cur / el
        pct = (cur * 100 / self.total) if self.total else 0
        w, f = 28, int(28 * pct / 100) if self.total else 0
        bar = "=" * f + ">" + " " * max(0, w - f - 1)
        rem = max(self.total - cur, 0) if self.total else 0
        eta = rem / sp if sp > 0 and self.total else 0
        print(f"\r{self.label}: [{bar}] {pct:6.2f}% {format_bytes(cur)}/{format_bytes(total)} "
              f"{format_bytes(int(sp))}/s ETA {int(eta)//60:02d}:{int(eta)%60:02d}",
              end="", flush=True)
        if self.total and cur >= self.total:
            print()


async def _login_cli(client) -> None:
    from .config import load_settings as _load, save_settings as _save
    await client.connect()
    if await client.is_user_authorized():
        me = await client.get_me()
        # Identity pinning: a planted session for a different account is
        # refused instead of auto-trusted.
        try:
            pinned = (_load().user_id or "").strip() if hasattr(_load(), "user_id") else ""
            me_id = str(getattr(me, "id", "") or "")
            if pinned and me_id and pinned != me_id:
                try:
                    await client.log_out()
                except Exception:
                    pass
                try:
                    await client.disconnect()
                except Exception:
                    pass
                from .config import clear_session as _clear
                _clear()
                raise RuntimeError(
                    "Session identity changed (different Telegram account). "
                    "Signed out for safety — please log in again.")
        except RuntimeError:
            raise
        except Exception:
            pass
        from .config import _sanitize_display as _sd
        print(f"Logged in as: {_sd(str(getattr(me, 'first_name', None) or getattr(me, 'username', None) or 'Telegram user'))}")
        # Pin the identity on first successful auto-login.
        try:
            st = _load()
            me_id = str(getattr(me, "id", "") or "")
            if me_id and getattr(st, "user_id", None) != me_id:
                st.user_id = me_id
                _save(st)
        except Exception:
            pass
        return
    print("No session found — one-time login.")
    try:
        # Hidden input: a bot token is a full secret and must not echo to
        # the terminal/scrollback. Phone numbers are hidden too as a result.
        phone = getpass.getpass("Phone (international, e.g. +8801XXXXXXXXX) or bot token (input hidden — type carefully): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise RuntimeError("Login cancelled by user.")
    if not phone:
        raise RuntimeError("Phone/bot token required.")
    try:
        if tg_client.looks_like_phone(phone):
            await client.start(phone=phone)
        else:
            await client.start(bot_token=phone)
    except Exception as exc:
        from telethon import errors as terr
        if isinstance(exc, terr.FloodWaitError):
            raise RuntimeError(
                f"Telegram rate-limit: wait {exc.seconds}s before logging in.") from exc
        raise
    finally:
        try:
            del phone
        except NameError:
            pass
    try:
        me = await client.get_me()
        st = _load()
        me_id = str(getattr(me, "id", "") or "")
        if me_id:
            st.user_id = me_id
            _save(st)
    except Exception:
        pass
    harden_session_files()
    print("Login OK — session will be reused.")


async def _download_one(client, url: str, out_dir: Path, mode: str, custom_title: str = "") -> Path:
    from telethon import errors as terr
    try:
        chat, mid, thread = parse_message_url(url)
    except ValueError as exc:
        raise DownloadError(str(exc))
    try:
        msg = await asyncio.wait_for(client.get_messages(chat, ids=mid), timeout=120)
    except terr.FloodWaitError as exc:
        wait = min(int(exc.seconds) + 1, 300)
        print(f"Telegram asks to wait {wait}s...")
        await asyncio.sleep(wait)
        try:
            msg = await asyncio.wait_for(client.get_messages(chat, ids=mid), timeout=120)
        except terr.FloodWaitError as exc2:
            raise DownloadError(
                f"Telegram rate-limit ({exc2.seconds}s). Try again later.") from exc2
    if not msg:
        raise DownloadError("Message not found / no access.")
    if not getattr(msg, "media", None):
        raise DownloadError("Message has no downloadable media.")
    title = safe_filename(custom_title) if custom_title else auto_title_from_message(msg, chat)
    ext = infer_extension(msg)
    source = out_dir / f"{title}{ext}"
    target = out_dir / f"{title}.mp4"
    # Containment: a hostile title must not escape out_dir via `..` or a
    # pre-planted symlink. safe_filename strips separators, but verify.
    try:
        out_res = out_dir.resolve()
        for p in (source, target):
            try:
                if p.is_symlink():
                    raise DownloadError("Refusing to write through a symlink.")
                # Must be direct child of out_dir, not parent traversal
                if p.absolute().resolve().parent != out_res:
                    raise DownloadError("Refusing to write outside the download folder.")
            except DownloadError:
                raise
            except OSError:
                pass
    except DownloadError:
        raise
    except OSError:
        pass
    if mode in ("2", "3"):
        try:
            if source.resolve() == target.resolve():
                source = out_dir / f"{title}.source{ext}"
        except OSError:
            pass
    print(f"\nDownloading to: {_sanitize_display(str(source))}")
    await download_resumable(client, msg, source, progress_cb=_CliProgress(0, "Downloading"))
    if mode in ("2", "3"):
        if not converter.has_ffmpeg():
            print("ffmpeg missing — keeping original file. Install: " +
                  next((d.install_for_current_os() for d in deps_mod.check_all() if d.name.startswith("ffmpeg")), ""))
            return source
        import os as _os
        import secrets as _secrets
        # O_EXCL pre-create: close race where ffmpeg -y would follow a
        # symlink planted between exists check and exec.
        tmp = target.with_name(f"{target.stem}.part.{_os.getpid()}.{_secrets.token_hex(8)}.mp4")
        nofollow = getattr(_os, "O_NOFOLLOW", 0)
        try:
            fd = _os.open(tmp, _os.O_WRONLY | _os.O_CREAT | _os.O_EXCL | nofollow, 0o600)
            _os.close(fd)
            # ffmpeg will truncate; ensure file is 0600 and not a link
            if tmp.is_symlink():
                raise DownloadError("Refusing to publish through symlink.")
        except FileExistsError:
            raise DownloadError("Refusing unsafe converter temp file (exists).")
        except OSError as exc:
            import errno as _errno
            if exc.errno == _errno.ELOOP:
                raise DownloadError("Refusing to publish through symlink.") from exc
            raise
        print("Converting to MP4...")
        try:
            # Offload so event loop stays cancellable (was sync 1h block).
            import asyncio as _asyncio
            await _asyncio.to_thread(ffmpeg_to_mp4, source, tmp, mode)
        except BaseException:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        if tmp.is_symlink():
            try:
                tmp.unlink()
            except OSError:
                pass
            raise DownloadError("Refusing to publish converter output through symlink.")
        tmp.replace(target)
        if target.exists() and target.stat().st_size > 0:
            try:
                if not source.is_symlink():
                    source.unlink(missing_ok=True)
            except OSError:
                pass
        return target
    return source


# ---------------------------------------------------------------- commands

async def cmd_preview(urls: list[str]) -> int:
    if len(urls) > MAX_URLS:
        print(f"URL list capped at {MAX_URLS} (got {len(urls)}); extra URLs ignored.")
        urls = urls[:MAX_URLS]
    s = _ensure_api(load_settings())
    try:
        api_id = s.api_id_int()
    except ValueError as exc:
        _die(str(exc))
    client = tg_client.build_client(api_id, str(s.api_hash))
    try:
        await _login_cli(client)
        for u in urls:
            pv = await tg_client.fetch_preview(client, u)
            du = _sanitize_display(u)
            if pv.error or not pv.has_media:
                print(f"FAIL  {du}\n      {_sanitize_display(pv.error)}")
            else:
                print(f"OK    {_sanitize_display(pv.title)}{pv.ext}  ({format_bytes(pv.size)})  {du}")
    finally:
        await client.disconnect()
    return 0


async def cmd_download(urls: list[str], mode: str, yes: bool, out_dir_s: str = "") -> int:
    from telethon import errors as terr
    if len(urls) > MAX_URLS:
        print(f"URL list capped at {MAX_URLS} (got {len(urls)}); extra URLs ignored.")
        urls = urls[:MAX_URLS]
    s = _ensure_api(load_settings())
    s = _ensure_dl_dir(s)
    try:
        if out_dir_s:
            from .config import Settings as _S
            _probe = _S(api_id="x", api_hash="x", download_dir=out_dir_s)
            out_dir = ensure_download_dir(_probe)
        else:
            out_dir = ensure_download_dir(s)
    except (OSError, ValueError, RuntimeError) as exc:
        _die(f"Cannot use download folder: {exc}")
    mode = mode or s.output_mode or "1"
    if mode not in ("1", "2", "3"):
        _die("Mode must be 1, 2 or 3.")
    try:
        api_id = s.api_id_int()
    except ValueError as exc:
        _die(str(exc))
    client = tg_client.build_client(api_id, str(s.api_hash))
    try:
        await _login_cli(client)
        # Step 1: fetch titles (user asked: show titles, confirm before download).
        print(f"\nFetching {len(urls)} title(s)...")
        previews = [await tg_client.fetch_preview(client, u) for u in urls]
        ok = [p for p in previews if not p.error and p.has_media]
        bad = [p for p in previews if p.error or not p.has_media]
        for p in ok:
            print(f"  [ok] {_sanitize_display(p.title)}{p.ext}  ({format_bytes(p.size)})")
        for p in bad:
            print(f"  [skip] {_sanitize_display(p.url)} -> {_sanitize_display(p.error)}")
        if not ok:
            print("Nothing downloadable.")
            return 1
        if not yes:
            try:
                ans = input(f"\nDownload {len(ok)} file(s) to {out_dir}? [Y/n]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                print("Cancelled.")
                return 0
            if ans in ("n", "no", "q"):
                print("Cancelled.")
                return 0
        # Persist to queue (resume-all support) then download sequentially.
        store = QueueStore()
        store.extend([DownloadItem(url=p.url, title=p.title,
                                   ext=p.ext, size=p.size, status="queued") for p in ok])
        fails = 0
        for item in list(store.items[-len(ok):]):
            try:
                store.update(item.id, status="downloading")
                final = await _download_one(client, item.url, out_dir, mode, item.title)
                store.update(item.id, status="done", progress=100.0, dest=str(final))
                print(f"\nSaved: {final}")
            except Cancelled as exc:
                store.update(item.id, status="cancelled",
                             error=_sanitize_display(str(exc)))
                print(f"\n{_sanitize_display(exc)}")
            except (DownloadError, RuntimeError, terr.RPCError) as exc:
                fails += 1
                store.update(item.id, status="error",
                             error=_sanitize_display(str(exc)))
                print(f"\nFailed {_sanitize_display(item.url)}: {_sanitize_display(exc)}")
        print(f"\nDone: {len(ok)-fails} ok, {fails} failed. Queue saved; rerun to resume.")
        return 1 if fails else 0
    finally:
        await client.disconnect()


def cmd_config(args) -> int:
    s = load_settings()
    if args.config_action == "show":
        print(f"config dir : {_redact_home(config_dir_str())}")
        print(f"api_id     : {_mask_id(s.api_id)}")
        print(f"api_hash   : {'********' if s.api_hash else '(not set)'}")
        print(f"download   : {_redact_home(s.download_dir) or '(not set — will ask on first run)'}")
        print(f"mode       : {s.output_mode} (1=original, 2=fast, 3=max)")
        print(f"theme      : {s.theme} (system/light/dark)")
        return 0
    if args.config_action == "set":
        for kv in args.kv or []:
            if "=" not in kv:
                print(f"Ignoring {kv!r} (use key=value).")
                continue
            k, v = kv.split("=", 1)
            k, v = k.strip(), v.strip()
            if k == "api_hash":
                # Secrets via argv leak to shell history and process lists.
                print("Refusing api_hash via command line (it would leak to shell "
                      "history and `ps`). Use TG_API_HASH env or enter it "
                      "interactively on the next run instead.")
                continue
            if k == "api_id":
                print("Refusing api_id via command line (it would leak to shell "
                      "history and `ps`). Enter it interactively on the next run.")
                continue
            if k in ("download_dir", "output_mode", "theme"):
                if k == "download_dir":
                    # Validate containment now (refuses config-dir overlap,
                    # root, symlinks) instead of failing at download time.
                    try:
                        from .config import Settings as _S
                        ensure_download_dir(_S(api_id="x", api_hash="x",
                                              download_dir=v))
                    except ValueError as exc:
                        print(f"Refusing download_dir: {exc}")
                        continue
                    except OSError as exc:
                        print(f"Cannot use download folder: {exc}")
                        continue
                if k == "output_mode" and v not in ("1", "2", "3"):
                    print("output_mode must be 1, 2 or 3.")
                    continue
                if k == "theme" and v not in ("system", "light", "dark"):
                    print("theme must be system, light or dark.")
                    continue
                setattr(s, k, v)
            else:
                print(f"Unknown key {k!r} (valid: download_dir, output_mode, theme).")
        save_settings(s)
        print("Saved.")
        return 0
    if args.config_action == "path":
        print(config_dir_str())
        return 0
    if args.config_action == "reset":
        from .config import QUEUE_FILE, Settings
        save_settings(Settings())
        try:
            QUEUE_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        clear_session()
        print("Config reset (settings, queue and session cleared). "
              "Next run will ask for API + folder again.")
        return 0
    if args.config_action == "logout":
        async def _logout_go():
            st = load_settings()
            if not st.api_id or not st.api_hash:
                return False
            try:
                c = tg_client.build_client(st.api_id_int(), str(st.api_hash))
            except (ValueError, TypeError):
                return False
            try:
                await c.connect()
                try:
                    await c.log_out()
                    return True
                except Exception:
                    return False
            finally:
                try:
                    await c.disconnect()
                except Exception:
                    pass
        try:
            server_ok = asyncio.run(_logout_go())
        except Exception:
            server_ok = False
        # Clear the pinned identity regardless so a stale pin can't lock out
        # the next login.
        try:
            st = load_settings()
            if getattr(st, "user_id", None):
                st.user_id = None
                save_settings(st)
        except Exception:
            pass
        local_ok = clear_session()
        leftover = False
        try:
            from .config import CONFIG_DIR as _CD
            leftover = any(_CD.glob("session*"))
        except OSError:
            pass
        if leftover:
            print("Warning: session files remain (locked?). "
                  "Server logout "
                  + ("succeeded." if server_ok else "FAILED — revoke at https://my.telegram.org."))
            return 1
        if not server_ok:
            print("Local session removed, but server logout FAILED (offline?). "
                  "Revoke the session at https://my.telegram.org if the device was shared.")
            return 1
        print("Logged out (server session revoked, local files removed)." if local_ok
              else "Logged out (server session revoked, no local session file).")
        return 0
    if args.config_action == "login":
        async def _go():
            st = _ensure_api(load_settings())
            c = tg_client.build_client(st.api_id_int(), str(st.api_hash))
            try:
                await _login_cli(c)
            finally:
                await c.disconnect()
        asyncio.run(_go())
        return 0
    return 0


def _app_version() -> str:
    try:
        from importlib.metadata import version
        return version("telegram-downloader")
    except Exception:
        return __version__


def cmd_update(args) -> int:
    from . import updates
    print(f"Current version: v{_app_version()}  (checking {updates.REPO}...)")
    info = updates.check_for_update()
    if info is None:
        print("Could not reach the update server (offline or no releases yet).")
        return 1
    print(f"Latest release: v{info.latest_version}  {info.release_url}")
    if not info.is_newer_than(_app_version()):
        print(f"You are up to date (v{_app_version()}).")
        return 0
    asset = info.asset_for_platform()
    print(f"\nUpdate available: v{_app_version()} -> v{info.latest_version}")
    if asset:
        print(f"Installer for your OS: {asset.name}\n  {asset.url}")
    else:
        print("No installer asset for your OS in this release.")
    print(f"Open the release page to upgrade: {info.release_url}")
    return 0


def cmd_deps(args) -> int:
    rows = deps_mod.check_all()
    print(f"{'DEPENDENCY':34} {'STATUS':10} DETAIL")
    for d in rows:
        mark = "OK  " if d.ok else "MISS"
        print(f"{d.name:34} {mark:10} {d.version or d.hint}")
        if not d.ok:
            print(f"  -> install ({__import__('platform').system()}): {d.install_for_current_os()}")
    print("\nFull installer script: ./scripts/install-deps.sh")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tg-dl", description=f"Telegram Downloader v{__version__}")
    p.add_argument("-V", "--version", action="version", version=f"tg-dl {_app_version()}")
    sub = p.add_subparsers(dest="cmd", required=False)

    g = sub.add_parser("gui", help="Launch the desktop GUI (Flet).")
    g.add_argument("--port", type=int, default=0, help="Flet port (0=auto).")

    d = sub.add_parser("download", help="Fetch titles, confirm, then download URL(s).")
    d.add_argument("urls", nargs="+", help="One or more t.me message URLs.")
    d.add_argument("--mode", default="", help="1=original, 2=fast, 3=max (default: settings).")
    d.add_argument("--dir", default="", help="Override download folder for this run.")
    d.add_argument("-y", "--yes", action="store_true", help="Skip confirm prompt.")

    b = sub.add_parser("batch", help="Download every URL listed in a .txt file (max 256 KB, max 100 URLs).")
    b.add_argument("file", help="Text file with one URL per line (max 256 KB).")
    b.add_argument("--mode", default="", help="1=original, 2=fast, 3=max (default: settings).")
    b.add_argument("--dir", default="", help="Override download folder for this run.")
    b.add_argument("-y", "--yes", action="store_true", help="Skip confirm prompt.")

    pv = sub.add_parser("preview", help="Only fetch + show titles (no download).")
    pv.add_argument("urls", nargs="+")

    c = sub.add_parser("config", help="Show/edit settings, login/logout.")
    c.add_argument("config_action", nargs="?", default="show",
                   choices=["show", "set", "path", "reset", "login", "logout"])
    c.add_argument("kv", nargs="*", help="key=value pairs for 'set'.")

    dp = sub.add_parser("deps", help="Check dependencies + per-OS install commands.")

    up = sub.add_parser("update", help="Check for a newer release on GitHub.")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not args.cmd or args.cmd == "gui":
        try:
            from .gui import run_gui
            port = getattr(args, "port", 0) or 0
            run_gui(port=port or None)
            return 0
        except (ImportError, ModuleNotFoundError):
            print("GUI needs the 'gui' extra:  pip install -e '.[gui]'\n"
                  "Or use CLI:  tg-dl download <url>  |  tg-dl deps", file=sys.stderr)
            return 1
    if args.cmd == "preview":
        urls = split_urls(" ".join(args.urls)) or args.urls
        return asyncio.run(cmd_preview(urls))
    if args.cmd == "download":
        urls = split_urls(" ".join(args.urls)) or args.urls
        return asyncio.run(cmd_download(urls, args.mode, args.yes, args.dir))
    if args.cmd == "batch":
        import os as _os
        import stat as _stat
        try:
            p = Path(args.file)
            # No symlink / fifo bomb, no TOCTOU: open O_NOFOLLOW|O_RDONLY
            # first, then fstat the fd (a swapped-in link raises ELOOP and
            # the type/size checks run on the opened file itself).
            nofollow = getattr(_os, "O_NOFOLLOW", 0)
            try:
                fd = _os.open(p, _os.O_RDONLY | nofollow)
            except OSError as exc:
                import errno as _errno
                if exc.errno == _errno.ELOOP:
                    _die("Batch file must be a regular file (symlink refused).")
                _die(f"Cannot read batch file: {exc}")
            try:
                st = _os.fstat(fd)
                if not _stat.S_ISREG(st.st_mode):
                    _die("Batch file must be a regular file.")
                if st.st_size > MAX_BATCH_BYTES:
                    _die("Batch file too large (max 256 KB).")
                with _os.fdopen(fd, "r", encoding="utf-8", errors="replace") as fh:
                    fd = -1
                    raw = fh.read(257 * 1024)
            finally:
                if fd != -1:
                    try:
                        _os.close(fd)
                    except OSError:
                        pass
        except SystemExit:
            raise
        except (OSError, ValueError) as exc:
            _die(f"Cannot read batch file: {exc}")
        urls = split_urls(raw)
        if not urls:
            _die("No t.me URLs found in batch file.")
        print(f"Loaded {len(urls)} URL(s) from {args.file}")
        return asyncio.run(cmd_download(urls, args.mode, args.yes, args.dir))
    if args.cmd == "config":
        return cmd_config(args)
    if args.cmd == "deps":
        return cmd_deps(args)
    if args.cmd == "update":
        return cmd_update(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# Telegram Downloader — GUI + CLI

Persistent login, resumable downloads, queue with resume-all, and MP4 conversion.
Built from the original single-file `downloader.py`, refactored into a proper app.

![icon](assets/icon.svg)

## Features (as requested)

1. **Single or multiple URLs** — paste 1..N `t.me` links, or load a `.txt` batch file.
2. **Fetch titles → confirm → download** — the app previews each title/size first and asks for confirmation.
3. **Dedicated Settings** — API ID/hash are mandatory, asked once on first run, editable in Settings / `tg-dl config`.
4. **Built-in dependency checks** — Dependencies page / `tg-dl deps` shows OK/MISSING plus the exact install command for your OS (apt / dnf / pacman / brew / winget / Termux `pkg`).
5. **Queue + resume-all** — progress persists in `queue.json`; interrupted downloads resume from partial files.
6. **Batch `.txt`, config commands, per-OS installers, `.deb` / `.dmg` / `.apk` builders.**
7. **In-app update checker** — Settings → About shows the installed version (from package metadata, never hardcoded) plus engine versions, and `Check for Updates` compares against GitHub releases with Later / Open Release Page / Download Installer actions (`tg-dl update` in CLI).
8. **Theme** — dark by default with a Dark Theme switch in Settings (persisted, applied at startup).

## Quick start (Linux)

```bash
git clone https://github.com/MDHasan0078/telegram-downloader
cd telegram-downloader
./scripts/install-deps.sh --gui --speed   # installs python, ffmpeg, pip package
tg-dl deps                                 # verify
tg-dl gui                                  # launch desktop GUI
```

First run: Settings asks for **API ID + hash** (from https://my.telegram.org → API development tools)
and **download folder** (defaults to `~/Downloads/Telegram`). Saved privately (`0600`).

macOS: `brew install python ffmpeg` then same pip steps.
Android/Termux: `pkg install python ffmpeg` then `pip install -e ".[gui]"`.

## GUI tour (dark-first Material3, purple seed — same design language as simple-yt-downloader)

- **Add**: header + **Add Download** card (link field, Original/Fast/Max segmented mode, Fetch titles) → preview cards (tick/edit titles, per-row errors) → Download selected → confirm dialog → auto-moves to Queue. **Quick Actions** card: Open folder, Check dependencies, Settings.
- **Queue**: `Downloads (n)` header + Clear finished; empty state when idle; per-item cards with state icon (grey queued, purple downloading, orange converting, green done, red error), title + size, `%` pill while active, progress bar, speed/ETA, red error box, Retry / Remove actions; Resume all / Start + Cancel batch (partial kept).
- **Settings**: header + **Connection** card (API ID/hash, folder + Browse, Save, Login/verify, Logout), **Download Defaults** card (folder + mode display, mode edited on Add tab), **Appearance** card (Dark Theme switch, on by default), **About** card (metadata version, Telethon/FFmpeg rows, Check for Updates), centered Reset to Defaults.
- **Dependencies**: header + **Status** card (green/red state icon, name, version/hint, selectable install command for missing items) + Re-check.

Supported links:

```text
https://t.me/channel/123
https://t.me/channel/244/264        (thread/topic)
https://t.me/c/1234567890/123
https://t.me/c/1234567890/244/264
```

## CLI

```bash
tg-dl gui                                   # GUI
tg-dl --version                             # installed version
tg-dl update                                # check GitHub releases for a newer version
tg-dl preview https://t.me/ch/1 ...         # only fetch titles
tg-dl download https://t.me/ch/1 ...        # fetch → confirm → download
tg-dl download -y --mode 2 --dir ~/Videos URL...
tg-dl batch urls.txt -y                     # one URL per line
tg-dl config show | set | path | login | logout | reset
tg-dl config set download_dir=/mnt/drive output_mode=2
tg-dl deps                                  # checks + install commands
python -m telegram_downloader gui           # same as tg-dl gui
```

Output modes: `1` Original (fastest, no ffmpeg) · `2` Fast compression (veryfast CRF28, remux if already H.264/AAC) · `3` Max compression (medium CRF28).

## Project layout

```text
src/telegram_downloader/
  config.py          settings (~/.config/telegram-downloader/config.json, 0600)
  telegram_utils.py  URL parse, filenames, titles (unit-tested)
  tg_client.py       Telethon client + preview/login
  downloader.py      resumable 512 KiB chunk downloader (progress callback)
  converter.py       ffmpeg/ffprobe wrapper
  deps.py            per-OS dependency checker
  updates.py         GitHub-release update checker (stdlib only)
  queue_store.py     queue.json persistence (0600)
  cli.py             tg-dl command
  gui.py             Flet GUI (Downloads/Queue/Settings/Dependencies)
tests/               pytest, no network
scripts/             install-deps.sh, build-deb.sh, build-dmg.sh, build-apk.sh
```

## Packaging: .deb / .dmg / .apk

| Target | How | Notes |
|---|---|---|
| Linux `.deb` | `./scripts/build-deb.sh` → `dist/*.deb`, install `sudo dpkg -i dist/*.deb` | Runs on Linux; bundles venv + `tg-dl gui` desktop entry |
| macOS `.dmg` | On a Mac: `./scripts/build-dmg.sh` (Flet → macos bundle → `create-dmg`) | Cannot build macOS bundle from Linux |
| Android `.apk` | `./scripts/build-apk.sh [--debug]` (Flet → Flutter → APK, `build/apk/`) | First build downloads Flutter/SDK; CI has an `apk` job |

GitHub Actions (`.github/workflows/build.yml`) builds + uploads all three on tags `v*`.

## Releasing a new version

Single source of truth is `src/telegram_downloader/__init__.py` (`__version__`) plus the `version =` line in `pyproject.toml` (keep them in sync):

```bash
# 1. Bump version in both files, commit, push to main
# 2. Tag and push the tag — CI stamps the .deb, builds, and attaches
#    artifacts to the GitHub Release (which powers the in-app updater):
git tag v0.2.0 && git push origin v0.2.0
```

Release-asset naming (the updater looks for these first, then any `.deb`/`.dmg`/`.apk`):
`telegram-downloader_<ver>_amd64.deb` · `telegram-downloader_<ver>.dmg` · `telegram-downloader_<ver>.apk`

## Troubleshooting

| Symptom | Fix |
|---|---|
| `externally-managed-environment` | Use venv: `python3 -m venv .venv && .venv/bin/pip install -e ".[gui]"` or run `scripts/install-deps.sh` |
| Phone invalid | Full international format, e.g. `+8801XXXXXXXXX` |
| Login every run | Finish one login; check `~/.config/telegram-downloader/session.session` exists; `tg-dl config logout` then login again if invalid |
| Transfer timeout / resume | Keep partial file; rerun or Queue → Resume all (exponential backoff + reconnect built in) |
| ffmpeg missing | Mode 1 works; install per Dependencies page or `scripts/install-deps.sh` |
| Channel private / not found | Same account must see the link in Telegram; check thread vs message id order |
| GUI won't start | `pip install -e ".[gui]"` then `tg-dl gui`; see `tg-dl deps` |

## Security

- `api_hash` + `session.session` = full account access. Never commit `~/.config/telegram-downloader/`, never share.
- Local file permissions are locked down automatically: config dir `0700`, `config.json` / `session*` / `queue.json` `0600` (Telethon/queue files are re-hardened on login/save).
- `tg-dl config show` masks the API hash; the GUI hash field is a password field; error messages never include credentials.
- These paths are git-ignored so they can never be committed by accident: `config.json`, `*.session*`, `queue.json`.
- If you expose the GUI over the network (port forward / tunnel) for remote review, anyone with the URL can use your saved login — treat the link like a password and shut the tunnel down afterwards.
- Download only media you are authorized to save; follow Telegram ToS + local law.

## Dev

```bash
pip install -e ".[test]" && python -m pytest -q
tg-dl deps && tg-dl config show
```

# Telegram Downloader

<p align="center">
  <img src="assets/icon.svg" width="96" alt="Telegram Downloader icon">
</p>

<p align="center">
  Download media from Telegram with a desktop GUI or a CLI — persistent login, resumable downloads, batch queue, and MP4 conversion.
</p>

<p align="center">
  <a href="https://github.com/MDHasan0078/telegram-downloader/releases"><img src="https://img.shields.io/github/v/release/MDHasan0078/telegram-downloader" alt="Latest release"></a>
  <a href="https://github.com/MDHasan0078/telegram-downloader/actions"><img src="https://img.shields.io/github/actions/workflow/status/MDHasan0078/telegram-downloader/build.yml" alt="Build status"></a>
  <img src="https://img.shields.io/badge/python-%3E%3D3.9-blue" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT license">
</p>

---

## Features

- **Single or batch links** — paste one or many `t.me` URLs, or load a `.txt` batch file.
- **Preview before download** — fetch titles and sizes first, confirm, then download.
- **First-run setup** — API ID/hash asked once, editable later in Settings or `tg-dl config`.
- **Dependency checker** — built-in page (`tg-dl deps`) shows what's installed plus the exact install command for your OS.
- **Queue with resume-all** — progress persists in `queue.json`; interrupted downloads resume from partial files.
- **MP4 conversion** — Original (fastest) · Fast (veryfast CRF28) · Max (medium CRF28) via ffmpeg.
- **In-app updates** — checks GitHub releases, with Later / Open page / Download actions.
- **Dark theme** — dark by default, switchable in Settings.

## Install

Get the latest release for your platform from the
[**Releases**](https://github.com/MDHasan0078/telegram-downloader/releases) page
(`.deb` · `.apk` · macOS `.zip`), or build from source:

```bash
git clone https://github.com/MDHasan0078/telegram-downloader
cd telegram-downloader
./scripts/install-deps.sh --gui --speed   # python, ffmpeg, pip package
tg-dl deps                                # verify everything is installed
tg-dl gui                                 # launch the desktop GUI
```

First run asks for your **API ID + API hash** (get them at
[my.telegram.org](https://my.telegram.org) → API development tools) and a
**download folder** (default `~/Downloads/Telegram`). Credentials are stored
privately with `0600` permissions.

| Platform | Notes |
|---|---|
| Linux | Script above, or `./scripts/build-deb.sh` → `dist/*.deb` |
| macOS | `brew install python ffmpeg`, then the same pip steps |
| Android / Termux | `pkg install python ffmpeg`, then `pip install -e ".[gui]"` |

## Usage

### GUI

Three tabs, dark-first Material 3 design:

- **Add** — paste links (or load `.txt`), pick Original / Fast / Max, fetch titles,
  tick or edit them, confirm and download.
- **Queue** — live progress with speed/ETA, retry/remove per item, Resume all,
  cancel batch (partials are kept).
- **Settings** — connection (API ID/hash, folder, login/logout), download defaults,
  appearance, About with update checker. **Dependencies** tab shows install status
  with copy-paste commands.

Supported links:

```text
https://t.me/channel/123
https://t.me/channel/244/264        # thread / topic
https://t.me/c/1234567890/123
https://t.me/c/1234567890/244/264
```

### CLI

```bash
tg-dl gui                                   # launch GUI
tg-dl --version                             # installed version
tg-dl update                                # check GitHub releases
tg-dl preview <URL>...                      # fetch titles only
tg-dl download <URL>...                     # fetch → confirm → download
tg-dl download -y --mode 2 --dir ~/Videos <URL>...
tg-dl batch urls.txt -y                     # one URL per line
tg-dl config show | set | path | login | logout | reset
tg-dl config set download_dir=/mnt/drive output_mode=2
tg-dl deps                                  # dependency check
python -m telegram_downloader gui           # same as: tg-dl gui
```

## Project layout

```text
src/telegram_downloader/
  config.py          settings (~/.config/telegram-downloader/config.json, 0600)
  telegram_utils.py  URL parsing, filenames, titles (unit-tested)
  tg_client.py       Telethon client, preview, login
  downloader.py      resumable chunk downloader with progress callback
  converter.py       ffmpeg / ffprobe wrapper
  deps.py            per-OS dependency checker
  updates.py         GitHub-release update checker (stdlib only)
  queue_store.py     queue.json persistence (0600)
  cli.py             tg-dl command
  gui.py             Flet GUI (Add / Queue / Settings / Dependencies)
tests/               pytest, no network required
scripts/             install-deps.sh, build-deb.sh, build-dmg.sh, build-apk.sh
```

## Releases

Pushing a `v*` tag runs [CI](.github/workflows/build.yml): tests, then `.deb` /
`.apk` / macOS builds attached to the
[GitHub Release](https://github.com/MDHasan0078/telegram-downloader/releases)
(which also powers the in-app updater):

```bash
git tag v0.2.0 && git push origin v0.2.0
```

Version source of truth: `__version__` in
`src/telegram_downloader/__init__.py` plus the `version =` line in
`pyproject.toml` (CI stamps both from the tag for release builds).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `externally-managed-environment` | Use a venv (`python3 -m venv .venv`) or run `scripts/install-deps.sh` |
| Phone invalid | Use full international format, e.g. `+8801XXXXXXXXX` |
| Login every run | Finish one login; check `~/.config/telegram-downloader/session.session` exists; `tg-dl config logout` then log in again |
| Transfer timeout / resume | Keep the partial file; rerun or Queue → Resume all (backoff + reconnect built in) |
| ffmpeg missing | Mode 1 works without it; install via the Dependencies page or `scripts/install-deps.sh` |
| Private channel / not found | The same account must be able to open the link in Telegram; check thread vs message id order |
| GUI won't start | `pip install -e ".[gui]"`, then `tg-dl gui`; see `tg-dl deps` |

## Security

- `api_hash` + `session.session` = full account access. Never commit
  `~/.config/telegram-downloader/`, never share it.
- Permissions are locked down automatically: config dir `0700`;
  `config.json` / `session*` / `queue.json` at `0600` (re-hardened on login/save).
- Secrets are masked in `tg-dl config show`, the GUI hash field, and all error messages;
  these paths are git-ignored so they can't be committed by accident.
- If you expose the GUI over the network (port forward / tunnel), anyone with the URL
  can use your saved login — treat the link like a password and close the tunnel after.
- Download only media you're authorized to save; follow the Telegram ToS and local law.

## Development

```bash
pip install -e ".[test]" && python -m pytest -q
tg-dl deps && tg-dl config show
```

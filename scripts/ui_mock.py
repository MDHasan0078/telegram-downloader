#!/usr/bin/env python3
"""Dot-matrix mockup of the Telegram Downloader GUI (Material3-style).

Run:  python3 scripts/ui_mock.py
Pure stdlib, no dependencies — renders every screen with .(dots).
Mirrors simple-yt-downloader: dark-first purple seed #6750A4, header
blocks (tinted icon tile + bold title + subtitle), section cards
(20px padding, icon + title + subtitle), info rows (primary icon +
label/value), state rows (% pills, progress, byte details, error boxes).
"""
W = 66


def bar():
    print("." * W)


def line(text=""):
    text = f".. {text}" if text else ".."
    print(text.ljust(W - 2) + ".." if len(text) < W - 1 else text[:W])


def header(active):
    print()
    bar()
    line("TELEGRAM DOWNLOADER ............................. v0.1.0 ...")
    bar()
    tabs = []
    for key, label in [("add", "Add"), ("queue", "Queue"),
                       ("settings", "Settings"), ("deps", "Deps")]:
        tabs.append(f"[{label.upper()}]" if key == active else f" {label} ")
    line(".. " + " ... ".join(tabs) + " " + "." * 8)
    bar()


def add():
    header("add")
    line("[#] Telegram Downloader ....................................")
    line("Paste t.me links, fetch titles, then queue downloads ......")
    line("..........................................................")
    line("[> Add Download ] One link per line, fetch first ..........")
    line("Telegram link(s) .. nothing downloads till confirmed ......")
    line("[https://t.me/channel/123 ..............................] .")
    line("Mode: [Original] (Fast) (Max) .............................")
    line("[Fetch titles] .. [Load .txt] .. [Clear] ..................")
    line("(i) Found 2/3 downloadable. Tick + edit, then Download ....")
    bar()
    line("[= Preview . confirm before download ] Tick + edit ........")
    line("2 downloadable . 1 skipped ...... [All] .. [None] .........")
    line("[x] Introduction to Linux ..... .mp4 . 450.2 MB ..........")
    line("[x] Day-4 Networking .......... .mp4 . . 1.2 GB ..........")
    line("[!] https://t.me/broken ... Skipped: no access ...........")
    line("2 of 2 selected .................. [Download selected] ....")
    bar()
    line("[* Quick Actions ] Common destinations and checks .........")
    line("[Open folder] .. [Check dependencies] .. [Settings] ......")
    bar()


def queue():
    header("queue")
    line("[#] Queue .................................................")
    line("Track progress, retry errors, clear finished ..............")
    line("[ Downloads (3) ...................... [Clear finished] ...")
    line("..........................................................")
    line("3 total . 1 active . 1 queued . 1 done . 1 need attention .")
    line("[Resume all / Start] .. [Cancel batch] ...................")
    bar()
    line("(v) Introduction to Linux.mp4 ............ [38.4%] .......")
    line("[#############>.................................] ........")
    line("173.2 MB / 450.2 MB ... downloading... ...................")
    bar()
    line("( ) Day-4 Networking.mp4 .................... queued .....")
    line("Queued . waiting to start ................................")
    line(".......................................... [Remove] .....")
    bar()
    line("(x) Old Lecture.mp4 ........................... error .....")
    line("Failed . see details below ...............................")
    line("[!] Transfer interrupted . partial kept ..................")
    line("................................. [Retry] .. [Remove] ...")
    bar()
    line("(o) Saved Talk.mp4 ............................. done .....")
    line("(o) /root/Downloads/Telegram/Saved Talk.mp4 ..............")
    bar()


def settings():
    header("settings")
    line("[#] Settings ...............................................")
    line("Credentials, folders, appearance and updates ..............")
    line("[> Connection ] Asked once, stored privately (0600) ......")
    line("API asked once . stored privately as 0600 ................")
    line("API ID .. [1234567.....................................] .")
    line("API hash  [****************] masked .....................")
    line("Folder .. [/root/Downloads/Telegram..........] [Browse] .")
    line("[Save] .. [Login / verify] .. [Logout] ...................")
    bar()
    line("[= Download Defaults ] Used for new downloads .............")
    line("Output Directory ... /root/Downloads/Telegram ............")
    line("Output Mode ........ Fast (change it on the Add tab) .....")
    bar()
    line("[* Appearance ] Dark-first, like the reference app ........")
    line("Dark Theme ... does not follow system theme ....... [ON] .")
    bar()
    line("[i About ] Version, engine and updater ....................")
    line("Version .. v0.1.0 ........................................")
    line("Engine ... Telethon 1.45.0 ...............................")
    line("FFmpeg ... 6.1.1 present .................................")
    line("[Check for Updates] ......................................")
    line("............... [Reset to Defaults] ......................")
    bar()


def deps():
    header("deps")
    line("[#] Dependencies ...........................................")
    line("Engine status for download + convert ......................")
    line("Linux .. Python 3.14.2 ...................................")
    line("[= Status ] Required tools for download + convert .........")
    line("(v) Python 3.9+ ............ 3.14.2 ............ OK ......")
    line("(x) ffmpeg ................. missing ........... MISSING .")
    line("[!] sudo apt install ffmpeg .............................")
    line("(v) telethon ............... 1.45.0 ............ OK ......")
    line("[Re-check] ...............................................")
    bar()


def dialog():
    print()
    bar()
    line(".......... UPDATE AVAILABLE ..............................")
    bar()
    line("Version v0.2.0 is available (you have v0.1.0) ............")
    line("Download the installer now, or open the release page .....")
    line("...... [Later] .. [Open Release Page] .. [Download] .....")
    bar()


if __name__ == "__main__":
    add()
    queue()
    settings()
    deps()
    dialog()

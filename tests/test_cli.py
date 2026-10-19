"""CLI parser/version tests."""
import pathlib
import tomllib

from telegram_downloader import cli

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_the_two_declared_versions_agree():
    """pyproject.toml and __init__.py must declare the SAME version.

    The README calls both of them the source of truth, and CI stamps both
    from the release tag, so a shipped artifact is always consistent. But
    the checked-in files are what a developer installs from, and the two
    have drifted before: the 1.3.1 release commit bumped __init__.py and
    the matching pyproject bump was forgotten. Nothing caught it because
    the release build stamps both from the tag anyway.

    The visible symptom is the in-app updater lying. It compares the
    LATEST release against the version the app reports, and the app
    reports the INSTALLED DISTRIBUTION's version -- so a checkout
    installed from a stale pyproject.toml says "you have 1.3.0" and
    nags the user to install 1.3.1, which they are already running.
    """
    pkg = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = pkg["project"]["version"]
    from telegram_downloader import __version__
    assert declared == __version__, (
        f"pyproject.toml says {declared} but __init__.py says "
        f"{__version__}; bump both in the same commit or the app reports "
        "the wrong version and the updater nags about a release you "
        "already have")


def test_cli_version_sources_are_aligned(monkeypatch):
    def fake_version(name):
        assert name == "telegram-downloader"
        return "9.8.7"

    monkeypatch.setattr(cli, "__version__", "0.0.0-dev")
    monkeypatch.setattr("importlib.metadata.version", fake_version)

    parser = cli.build_parser()
    version_action = next(a for a in parser._actions if a.dest == "version")
    assert parser.description == "Telegram Downloader v9.8.7"
    assert version_action.version == "tg-dl 9.8.7"
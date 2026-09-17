"""CLI parser/version tests."""
from telegram_downloader import cli


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
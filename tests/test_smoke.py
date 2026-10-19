"""Smoke test: the package imports, the version matches, and the CLI entry is
callable (acceptance use)."""

from apa import __version__
from apa.cli import main


def test_version():
    assert __version__ == "0.1.0"


def test_cli_status(capsys):
    rc = main(["status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "W1 complete" in out  # milestone marker, updated as work progresses

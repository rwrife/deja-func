"""Tests for the deja CLI scaffold (M1)."""

from __future__ import annotations

import subprocess
import sys

import pytest

from deja import __version__
from deja.cli import build_parser, main


def test_version_constant_is_sane() -> None:
    assert isinstance(__version__, str)
    # Looks like a semver-ish string.
    assert __version__.count(".") >= 2


def test_hello_prints_message(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["hello"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "deja-func" in out


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main([])
    out = capsys.readouterr().out
    assert exit_code == 0
    # Help output advertises the program name and available commands.
    assert "usage" in out.lower()
    assert "hello" in out


def test_parser_builds() -> None:
    parser = build_parser()
    assert parser.prog == "deja"


def test_version_flag_via_subprocess() -> None:
    """`deja --version` works through the actual module entry point."""
    result = subprocess.run(
        [sys.executable, "-m", "deja.cli", "--version"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert __version__ in result.stdout

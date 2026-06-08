from __future__ import annotations

import pytest

from auto_ai_cr import __version__
from auto_ai_cr.cli import main


def test_version_flag_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])

    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"auto-ai-cr {__version__}"


def test_help_command_prints_top_level_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["help"]) == 0

    output = capsys.readouterr().out
    assert "usage: auto-ai-cr" in output
    assert "install-monitor" in output


def test_help_command_prints_subcommand_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["help", "run"]) == 0

    output = capsys.readouterr().out
    assert "usage: auto-ai-cr run" in output
    assert "--scope" in output


def test_help_command_rejects_unknown_topic(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["help", "missing"]) == 1

    assert "unknown help topic: missing" in capsys.readouterr().err

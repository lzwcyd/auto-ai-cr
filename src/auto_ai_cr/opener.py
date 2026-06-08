from __future__ import annotations

import os
from pathlib import Path
import platform
import shlex
import subprocess

from .config import AppConfig


def maybe_open_report(config: AppConfig, report_path: Path) -> None:
    if not config.open_report_after_review:
        return
    try:
        open_report(report_path, config.report_open_command)
    except Exception:
        return


def open_report(report_path: Path, command: str = "") -> None:
    path = report_path.expanduser().resolve()
    if command.strip():
        _open_with_command(path, command)
        return
    _open_with_system_default(path)


def _open_with_command(path: Path, command: str) -> None:
    quoted_report = shlex.quote(str(path))
    if "{report}" in command:
        rendered = command.replace("{report}", quoted_report)
    else:
        rendered = f"{command} {quoted_report}"
    subprocess.Popen(
        rendered,
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _open_with_system_default(path: Path) -> None:
    system = platform.system()
    if system == "Darwin":
        subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    if system == "Windows":
        os.startfile(str(path))  # type: ignore[attr-defined]
        return
    subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

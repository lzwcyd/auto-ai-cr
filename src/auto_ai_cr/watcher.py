from __future__ import annotations

from pathlib import Path
import time
from typing import Callable

from .config import AppConfig
from .git_ops import GitError, head_sha


def watch_head(
    repo: Path,
    config: AppConfig,
    on_change: Callable[[str], None],
    once: bool = False,
) -> None:
    last = _safe_head(repo)
    if last is None:
        raise GitError("not a git repository or repository has no commits")

    while True:
        time.sleep(config.poll_interval_seconds)
        current = _safe_head(repo)
        if current and current != last:
            last = current
            on_change(current)
            if once:
                return


def _safe_head(repo: Path) -> str | None:
    try:
        return head_sha(repo)
    except Exception:
        return None

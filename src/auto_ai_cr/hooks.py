from __future__ import annotations

from pathlib import Path


HOOK = """#!/bin/sh
auto-ai-cr run --scope latest_commit || true
"""


def install_post_commit_hook(repo: Path) -> Path:
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / "post-commit"

    if hook_path.exists():
        existing = hook_path.read_text(encoding="utf-8", errors="replace")
        marker = "auto-ai-cr run --scope latest_commit"
        if marker in existing:
            return hook_path
        hook_path.write_text(existing.rstrip() + "\n\n" + HOOK, encoding="utf-8")
    else:
        hook_path.write_text(HOOK, encoding="utf-8")

    hook_path.chmod(0o755)
    return hook_path

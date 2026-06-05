from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any


CONFIG_FILE = ".auto-ai-cr.json"
CODEX_REVIEW_COMMAND = "codex review -"
CLAUDE_REVIEW_COMMAND = "claude -p --permission-mode dontAsk --output-format text"


@dataclass(frozen=True)
class ToolConfig:
    type: str
    command: str | None = None


@dataclass(frozen=True)
class AppConfig:
    scope: str = "latest_commit"
    base_branch: str = "master"
    tool: str = "print"
    tools: dict[str, ToolConfig] = field(default_factory=lambda: default_tools())
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    max_diff_chars: int = 120_000
    reports_dir: str = ".auto-ai-cr/reviews"
    poll_interval_seconds: float = 2.0
    write_notes: bool = True
    note_ref: str = "codex-cr"

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "AppConfig":
        tools = default_tools()
        tools.update({
            name: ToolConfig(
                type=str(value.get("type", name)),
                command=value.get("command"),
            )
            for name, value in data.get("tools", {}).items()
        })
        defaults = cls()
        return cls(
            scope=str(data.get("scope", defaults.scope)),
            base_branch=str(data.get("base_branch", defaults.base_branch)),
            tool=str(data.get("tool", defaults.tool)),
            tools=tools,
            include=list(data.get("include", defaults.include)),
            exclude=list(data.get("exclude", defaults.exclude)),
            max_diff_chars=int(data.get("max_diff_chars", defaults.max_diff_chars)),
            reports_dir=str(data.get("reports_dir", defaults.reports_dir)),
            poll_interval_seconds=float(
                data.get("poll_interval_seconds", defaults.poll_interval_seconds)
            ),
            write_notes=bool(data.get("write_notes", defaults.write_notes)),
            note_ref=str(data.get("note_ref", defaults.note_ref)),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "base_branch": self.base_branch,
            "tool": self.tool,
            "tools": {
                name: {
                    key: value
                    for key, value in {
                        "type": tool.type,
                        "command": tool.command,
                    }.items()
                    if value is not None
                }
                for name, tool in self.tools.items()
            },
            "include": self.include,
            "exclude": self.exclude,
            "max_diff_chars": self.max_diff_chars,
            "reports_dir": self.reports_dir,
            "poll_interval_seconds": self.poll_interval_seconds,
            "write_notes": self.write_notes,
            "note_ref": self.note_ref,
        }


def load_config(repo: Path) -> AppConfig:
    config_path = repo / CONFIG_FILE
    if not config_path.exists():
        return AppConfig()
    with config_path.open("r", encoding="utf-8") as fp:
        return AppConfig.from_mapping(json.load(fp))


def default_tools() -> dict[str, ToolConfig]:
    return {
        "print": ToolConfig(type="print"),
        "codex": ToolConfig(type="command", command=CODEX_REVIEW_COMMAND),
        "claude": ToolConfig(type="command", command=CLAUDE_REVIEW_COMMAND),
        "command": ToolConfig(type="command", command="cat"),
    }


def write_default_config(repo: Path, overwrite: bool = False) -> Path:
    config_path = repo / CONFIG_FILE
    if config_path.exists() and not overwrite:
        raise FileExistsError(f"{config_path} already exists")
    write_config(repo, AppConfig())
    return config_path


def write_config(repo: Path, config: AppConfig) -> Path:
    config_path = repo / CONFIG_FILE
    config_path.write_text(
        json.dumps(config.to_mapping(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return config_path

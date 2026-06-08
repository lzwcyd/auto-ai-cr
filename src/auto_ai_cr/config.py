from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any


CONFIG_FILE = ".auto-ai-cr.json"
CODEX_REVIEW_COMMAND = "codex review -"
CLAUDE_REVIEW_COMMAND = "claude -p --permission-mode dontAsk --output-format text"
CURSOR_REVIEW_COMMAND = "cursor-agent -p --output-format text"
CODEX_FIX_COMMAND = "codex exec --sandbox workspace-write --ask-for-approval never -"
CLAUDE_FIX_COMMAND = "claude -p --permission-mode acceptEdits --output-format text"
CURSOR_FIX_COMMAND = "cursor-agent -p --output-format text --trust"
DEFAULT_REPORTS_DIR = "~/.auto-ai-cr/reviews"


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
    fix_tool: str = "codex"
    fix_tools: dict[str, ToolConfig] = field(default_factory=lambda: default_fix_tools())
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    max_diff_chars: int = 120_000
    reports_dir: str = DEFAULT_REPORTS_DIR
    poll_interval_seconds: float = 2.0
    open_report_after_review: bool = False
    report_open_command: str = ""
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
        fix_tools = default_fix_tools()
        fix_tools.update({
            name: ToolConfig(
                type=str(value.get("type", name)),
                command=value.get("command"),
            )
            for name, value in data.get("fix_tools", {}).items()
        })
        defaults = cls()
        return cls(
            scope=str(data.get("scope", defaults.scope)),
            base_branch=str(data.get("base_branch", defaults.base_branch)),
            tool=str(data.get("tool", defaults.tool)),
            tools=tools,
            fix_tool=str(data.get("fix_tool", defaults.fix_tool)),
            fix_tools=fix_tools,
            include=list(data.get("include", defaults.include)),
            exclude=list(data.get("exclude", defaults.exclude)),
            max_diff_chars=int(data.get("max_diff_chars", defaults.max_diff_chars)),
            reports_dir=normalize_user_path(str(data.get("reports_dir", defaults.reports_dir))),
            poll_interval_seconds=float(
                data.get("poll_interval_seconds", defaults.poll_interval_seconds)
            ),
            open_report_after_review=bool(
                data.get("open_report_after_review", defaults.open_report_after_review)
            ),
            report_open_command=str(
                data.get("report_open_command", defaults.report_open_command)
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
            "fix_tool": self.fix_tool,
            "fix_tools": {
                name: {
                    key: value
                    for key, value in {
                        "type": tool.type,
                        "command": tool.command,
                    }.items()
                    if value is not None
                }
                for name, tool in self.fix_tools.items()
            },
            "include": self.include,
            "exclude": self.exclude,
            "max_diff_chars": self.max_diff_chars,
            "reports_dir": self.reports_dir,
            "poll_interval_seconds": self.poll_interval_seconds,
            "open_report_after_review": self.open_report_after_review,
            "report_open_command": self.report_open_command,
            "write_notes": self.write_notes,
            "note_ref": self.note_ref,
        }


def resolve_reports_dir(repo: Path, reports_dir: str) -> Path:
    path = Path(normalize_user_path(reports_dir)).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (repo / path).resolve()


def normalize_user_path(value: str) -> str:
    if value.startswith("～"):
        return "~" + value[1:]
    return value


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
        "cursor": ToolConfig(type="command", command=CURSOR_REVIEW_COMMAND),
        "command": ToolConfig(type="command", command="cat"),
    }


def default_fix_tools() -> dict[str, ToolConfig]:
    return {
        "codex": ToolConfig(type="command", command=CODEX_FIX_COMMAND),
        "claude": ToolConfig(type="command", command=CLAUDE_FIX_COMMAND),
        "cursor": ToolConfig(type="command", command=CURSOR_FIX_COMMAND),
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

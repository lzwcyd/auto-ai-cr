from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
from typing import Any

from .config import AppConfig, ToolConfig, resolve_reports_dir
from .git_ops import DiffResult, run_git
from .opener import maybe_open_report


@dataclass(frozen=True)
class ReviewIssue:
    id: str
    severity: str
    title: str
    file: str
    line: int | None
    description: str
    risk: str
    recommendation: str
    status: str = "open"

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": self.id,
            "severity": self.severity,
            "title": self.title,
            "file": self.file,
            "line": self.line,
            "description": self.description,
            "risk": self.risk,
            "recommendation": self.recommendation,
            "status": self.status,
        }


@dataclass(frozen=True)
class ReviewResult:
    report_path: Path
    issues_path: Path
    issues: list[ReviewIssue]
    exit_code: int


def build_prompt(repo: Path, diff: DiffResult) -> str:
    base_line = f"Base：{diff.base_branch}\n" if diff.scope == "branch_diff" else ""
    return f"""你是资深代码审查助手。请对下面 Git diff 做 Code Review。

审查重点按优先级处理：
1. 会导致线上故障、数据错误、安全问题、并发问题、兼容性问题的缺陷。
2. 明显遗漏的测试。
3. 只有明确收益时才提出可维护性建议。

输出要求：
1. 用中文输出，简洁直接，不要展开背景知识，不要重复 diff。
2. 先给人看的 Markdown，严格使用下面结构：

# CR 结果

## 结论
- 状态：通过 / 需修复 / 有风险
- 摘要：1 句话说明整体判断。
- 问题数：Critical x / Warning y / Suggestion z

## 必须处理
如果没有 Critical 或 Warning，写“无”。
每条最多 4 行：
### CR-001 标题
- 位置：path:line
- 问题：一句话说明。
- 风险：一句话说明。
- 建议：一句话说明。

## 可选建议
只列 suggestion；没有则写“无”。

## 测试建议
只列必要测试；没有则写“无”。

3. 最后必须输出一个机器可读 JSON 代码块，格式严格如下：

```auto-ai-cr-issues
{{
  "issues": [
    {{
      "id": "CR-001",
      "severity": "critical|warning|suggestion",
      "title": "一句话问题标题",
      "file": "相对仓库路径",
      "line": 123,
      "description": "问题描述",
      "risk": "风险",
      "recommendation": "建议修复方式"
    }}
  ]
}}
```

如果没有发现问题，issues 必须是空数组。不要在 JSON 代码块内写注释，不要使用 Markdown。
除 JSON 代码块外，整份人类可读 Review 尽量控制在 120 行以内。

仓库：{repo}
范围：{diff.scope}
{base_line}HEAD：{diff.head_sha}
主题：{diff.subject}
Diff 是否截断：{"是" if diff.truncated else "否"}

```diff
{diff.diff}
```
"""


def run_review(repo: Path, config: AppConfig, diff: DiffResult) -> ReviewResult:
    reports_dir = resolve_reports_dir(repo, config.reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = reports_dir / f"{timestamp}-{diff.head_sha[:12]}-{diff.scope}.md"

    tool = config.tools.get(config.tool)
    if tool is None:
        raise ValueError(f"unknown review tool: {config.tool}")

    prompt = build_prompt(repo, diff)
    issues_path = report_path.with_suffix(".issues.json")

    if tool.type == "print":
        report_path.write_text(prompt, encoding="utf-8")
        issues = _write_issues(issues_path, [])
        _write_notes(repo, config, diff, report_path)
        maybe_open_report(config, report_path)
        return ReviewResult(report_path=report_path, issues_path=issues_path, issues=issues, exit_code=0)
    if tool.type == "command":
        result = _run_command_tool(repo, tool, diff, prompt, report_path, issues_path)
        _write_notes(repo, config, diff, report_path)
        maybe_open_report(config, report_path)
        return result
    raise ValueError(f"unsupported tool type: {tool.type}")


def _run_command_tool(
    repo: Path,
    tool: ToolConfig,
    diff: DiffResult,
    prompt: str,
    report_path: Path,
    issues_path: Path,
) -> ReviewResult:
    if not tool.command:
        raise ValueError("command tool requires a command")
    _ensure_command_available(tool.command)

    command = _render_command_template(
        tool.command,
        {
            "repo": shlex.quote(str(repo)),
            "scope": shlex.quote(diff.scope),
            "base": shlex.quote(diff.base_branch),
            "head": shlex.quote(diff.head_sha),
            "report": shlex.quote(str(report_path)),
        },
    )
    completed = subprocess.run(
        command,
        cwd=repo,
        shell=True,
        input=prompt,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    report = _format_command_report(tool, diff, completed)
    report_path.write_text("\n".join(report) + "\n", encoding="utf-8")
    issues = _write_issues(issues_path, parse_issues(completed.stdout))
    return ReviewResult(
        report_path=report_path,
        issues_path=issues_path,
        issues=issues,
        exit_code=completed.returncode,
    )


def _format_command_report(
    tool: ToolConfig,
    diff: DiffResult,
    completed: subprocess.CompletedProcess[str],
) -> list[str]:
    output = completed.stdout.strip() or "未收到 Review 输出。"
    report = [
        "# Auto AI CR",
        "",
        f"> {diff.subject or diff.scope} · `{diff.head_sha[:12]}` · {diff.scope}",
        "",
        output,
        "",
        "<details>",
        "<summary>运行信息</summary>",
        "",
        f"- Tool: `{tool.command}`",
        f"- Exit code: `{completed.returncode}`",
        f"- Scope: `{diff.scope}`",
        f"- Base: `{diff.base_branch}`",
        f"- HEAD: `{diff.head_sha}`",
        f"- Diff truncated: `{'yes' if diff.truncated else 'no'}`",
        "",
        "</details>",
    ]
    if completed.stderr.strip():
        report.extend(
            [
                "",
                "<details>",
                "<summary>工具错误输出</summary>",
                "",
                "```text",
                completed.stderr.strip(),
                "```",
                "",
                "</details>",
            ]
        )
    return report


def _ensure_command_available(command: str) -> None:
    executable = _first_command_token(command)
    if not executable:
        return
    if _uses_shell_features(command):
        return
    if "/" in executable:
        if Path(executable).expanduser().exists():
            return
    elif shutil.which(executable):
        return
    raise ValueError(
        f"找不到 CR 工具 `{executable}`。请先安装它，或在 UI 的“外部命令”里配置完整路径。"
    )


def _first_command_token(command: str) -> str:
    try:
        parts = shlex.split(command)
    except ValueError:
        return ""
    return parts[0] if parts else ""


def _uses_shell_features(command: str) -> bool:
    return any(token in command for token in ["|", "&&", "||", ";", "$(", "`", "<(", ">(", "\n"])


def _render_command_template(command: str, values: dict[str, str]) -> str:
    rendered = command
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", value)
    return rendered


def parse_issues(text: str) -> list[ReviewIssue]:
    for candidate in _issue_json_candidates(text):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        issues = payload.get("issues", payload) if isinstance(payload, dict) else payload
        if isinstance(issues, list):
            return _normalize_issues(issues)
    return []


def _issue_json_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    for match in re.finditer(r"```([^\n`]*)\n(.*?)```", text, re.DOTALL):
        info = match.group(1).strip().lower()
        body = match.group(2).strip()
        if "auto-ai-cr-issues" in info:
            candidates.insert(0, body)
        elif "json" in info or body.startswith("{") or body.startswith("["):
            candidates.append(body)
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        candidates.append(stripped)
    return candidates


def _normalize_issues(raw_issues: list[Any]) -> list[ReviewIssue]:
    issues: list[ReviewIssue] = []
    for index, raw in enumerate(raw_issues, start=1):
        if not isinstance(raw, dict):
            continue
        issue_id = str(raw.get("id") or f"CR-{index:03d}")
        line = _optional_int(raw.get("line"))
        issues.append(
            ReviewIssue(
                id=issue_id,
                severity=_normalize_severity(str(raw.get("severity") or "warning")),
                title=str(raw.get("title") or issue_id),
                file=str(raw.get("file") or ""),
                line=line,
                description=str(raw.get("description") or ""),
                risk=str(raw.get("risk") or ""),
                recommendation=str(raw.get("recommendation") or ""),
                status=str(raw.get("status") or "open"),
            )
        )
    return issues


def _normalize_severity(value: str) -> str:
    severity = value.strip().lower()
    if severity in {"critical", "warning", "suggestion"}:
        return severity
    if severity in {"high", "error", "bug", "critical（必须修复）"}:
        return "critical"
    if severity in {"low", "nit", "note", "suggest"}:
        return "suggestion"
    return "warning"


def _optional_int(value: object) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _write_issues(path: Path, issues: list[ReviewIssue]) -> list[ReviewIssue]:
    payload = {"issues": [issue.to_mapping() for issue in issues]}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return issues


def _write_notes(repo: Path, config: AppConfig, diff: DiffResult, report_path: Path) -> None:
    if not config.write_notes or not config.note_ref:
        return
    try:
        run_git(
            repo,
            [
                "notes",
                f"--ref={config.note_ref}",
                "add",
                "-f",
                "-F",
                str(report_path),
                diff.head_sha,
            ],
        )
    except Exception:
        return

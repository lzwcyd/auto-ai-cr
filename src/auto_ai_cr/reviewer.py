from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
import shlex
import subprocess
from typing import Any

from .config import AppConfig, ToolConfig
from .git_ops import DiffResult, run_git


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

优先级：
1. 找出会导致线上故障、数据错误、安全问题、并发问题、兼容性问题的缺陷。
2. 找出明显遗漏的测试。
3. 只在有明确收益时提出可维护性建议。
4. 如果没有问题，请明确说明没有发现高置信问题。

输出格式：
1. 先输出给人看的 Markdown Review，按严重程度排序。每条问题包含文件/位置线索、风险、建议修复方式。
2. 最后必须输出一个机器可读 JSON 代码块，格式严格如下：

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
    reports_dir = (repo / config.reports_dir).resolve()
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
        return ReviewResult(report_path=report_path, issues_path=issues_path, issues=issues, exit_code=0)
    if tool.type == "command":
        result = _run_command_tool(repo, tool, diff, prompt, report_path, issues_path)
        _write_notes(repo, config, diff, report_path)
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
    report = [
        f"# Auto AI CR Report",
        "",
        f"- Tool: command",
        f"- Command: `{tool.command}`",
        f"- Exit code: {completed.returncode}",
        f"- Scope: {diff.scope}",
        f"- Base: {diff.base_branch}",
        f"- HEAD: {diff.head_sha}",
        "",
        "## Review Output",
        "",
        completed.stdout.strip() or "(empty stdout)",
    ]
    if completed.stderr.strip():
        report.extend(["", "## Tool Stderr", "", completed.stderr.strip()])
    report_path.write_text("\n".join(report) + "\n", encoding="utf-8")
    issues = _write_issues(issues_path, parse_issues(completed.stdout))
    return ReviewResult(
        report_path=report_path,
        issues_path=issues_path,
        issues=issues,
        exit_code=completed.returncode,
    )


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

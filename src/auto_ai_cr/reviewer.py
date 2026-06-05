from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import shlex
import subprocess

from .config import AppConfig, ToolConfig
from .git_ops import DiffResult, run_git


@dataclass(frozen=True)
class ReviewResult:
    report_path: Path
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
- 按严重程度排序。
- 每条问题包含文件/位置线索、风险、建议修复方式。
- 不要复述 diff。

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
    if tool.type == "print":
        report_path.write_text(prompt, encoding="utf-8")
        _write_notes(repo, config, diff, report_path)
        return ReviewResult(report_path=report_path, exit_code=0)
    if tool.type == "command":
        result = _run_command_tool(repo, tool, diff, prompt, report_path)
        _write_notes(repo, config, diff, report_path)
        return result
    raise ValueError(f"unsupported tool type: {tool.type}")


def _run_command_tool(
    repo: Path,
    tool: ToolConfig,
    diff: DiffResult,
    prompt: str,
    report_path: Path,
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
    return ReviewResult(report_path=report_path, exit_code=completed.returncode)


def _render_command_template(command: str, values: dict[str, str]) -> str:
    rendered = command
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", value)
    return rendered


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

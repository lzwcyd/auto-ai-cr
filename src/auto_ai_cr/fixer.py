from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import shlex
import subprocess

from .config import AppConfig, ToolConfig, resolve_reports_dir
from .git_ops import run_git
from .reviewer import ReviewIssue, _command_environment, _render_command_template


@dataclass(frozen=True)
class FixResult:
    output_path: Path
    exit_code: int
    diff: str
    status: str


@dataclass(frozen=True)
class FixPromptResult:
    prompt_path: Path
    prompt: str


def run_fix(
    repo: Path,
    config: AppConfig,
    issues: list[ReviewIssue],
    report_path: Path | None = None,
) -> FixResult:
    if not issues:
        raise ValueError("请选择至少一个问题再修复")

    tool = config.fix_tools.get(config.fix_tool)
    if tool is None:
        raise ValueError(f"unknown fix tool: {config.fix_tool}")
    if tool.type != "command":
        raise ValueError(f"unsupported fix tool type: {tool.type}")

    output_dir = (resolve_reports_dir(repo, config.reports_dir) / "fixes").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_path = output_dir / f"{timestamp}-{config.fix_tool}-fix.md"
    prompt = build_fix_prompt(repo, issues, report_path)
    completed = _run_command_tool(repo, tool, prompt, output_path)
    diff = _current_diff(repo)
    status = run_git(repo, ["status", "--short"], check=False)
    return FixResult(
        output_path=output_path,
        exit_code=completed.returncode,
        diff=diff,
        status=status,
    )


def save_fix_prompt(
    repo: Path,
    config: AppConfig,
    issues: list[ReviewIssue],
    report_path: Path | None = None,
) -> FixPromptResult:
    if not issues:
        raise ValueError("请选择至少一个问题再生成修复 Prompt")
    output_dir = (resolve_reports_dir(repo, config.reports_dir) / "fix-prompts").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    prompt_path = output_dir / f"{timestamp}-{config.fix_tool}-prompt.md"
    prompt = build_fix_prompt(repo, issues, report_path, agent=config.fix_tool)
    prompt_path.write_text(prompt, encoding="utf-8")
    return FixPromptResult(prompt_path=prompt_path, prompt=prompt)


def build_fix_prompt(
    repo: Path,
    issues: list[ReviewIssue],
    report_path: Path | None = None,
    agent: str = "agent",
) -> str:
    issue_payload = json.dumps(
        {"issues": [issue.to_mapping() for issue in issues]},
        ensure_ascii=False,
        indent=2,
    )
    report_line = f"CR 报告路径：{report_path}\n" if report_path else ""
    agent_line = _agent_instruction(agent)
    return f"""你是自动代码修复 agent。请在本地仓库中直接修改代码，只修复用户选中的 CR 问题。

约束：
1. 只修复下面 JSON 里的 selected issues，不要处理未选择的问题。
2. 不要做无关重构、格式化或大范围改写。
3. 如果某个问题无法安全修复，请在最终输出里说明原因，不要猜测式修改。
4. 修改完成后，请总结修改了哪些文件、每个 selected issue 如何被处理、是否运行了测试。
5. 不要自动提交 commit。
6. {agent_line}

仓库：{repo}
{report_line}
Selected issues:

```json
{issue_payload}
```
"""


def _agent_instruction(agent: str) -> str:
    if agent == "codex":
        return "面向 Codex CLI 执行：优先用最小补丁完成修复，并在结束前运行相关测试。"
    if agent == "claude":
        return "面向 Claude Code 执行：请先简述修复计划，再直接编辑文件并报告验证结果。"
    if agent == "cursor":
        return "面向 Cursor Agent 执行：请按 issue id 逐项修复，避免改动未选中的问题。"
    return "面向通用代码 Agent 执行：请直接修改本地文件，并给出清晰的验证说明。"


def issue_from_mapping(data: dict[str, object]) -> ReviewIssue:
    line = data.get("line")
    return ReviewIssue(
        id=str(data.get("id") or ""),
        severity=str(data.get("severity") or "warning"),
        title=str(data.get("title") or ""),
        file=str(data.get("file") or ""),
        line=int(line) if isinstance(line, int) or (isinstance(line, str) and line.isdigit()) else None,
        description=str(data.get("description") or ""),
        risk=str(data.get("risk") or ""),
        recommendation=str(data.get("recommendation") or ""),
        status=str(data.get("status") or "open"),
    )


def _run_command_tool(
    repo: Path,
    tool: ToolConfig,
    prompt: str,
    output_path: Path,
) -> subprocess.CompletedProcess[str]:
    if not tool.command:
        raise ValueError("fix command tool requires a command")
    command = _render_command_template(
        tool.command,
        {
            "repo": shlex.quote(str(repo)),
            "fix_report": shlex.quote(str(output_path)),
        },
    )
    completed = subprocess.run(
        command,
        cwd=repo,
        shell=True,
        env=_command_environment(command),
        input=prompt,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    report = [
        "# Auto AI CR Fix Report",
        "",
        f"- Tool: command",
        f"- Command: `{tool.command}`",
        f"- Exit code: {completed.returncode}",
        "",
        "## Agent Output",
        "",
        completed.stdout.strip() or "(empty stdout)",
    ]
    if completed.stderr.strip():
        report.extend(["", "## Agent Stderr", "", completed.stderr.strip()])
    output_path.write_text("\n".join(report) + "\n", encoding="utf-8")
    return completed


def _current_diff(repo: Path) -> str:
    return run_git(repo, ["diff", "--find-renames", "--stat", "--patch"], check=False)

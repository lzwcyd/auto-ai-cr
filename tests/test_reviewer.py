import os
import subprocess

from auto_ai_cr.git_ops import DiffResult
from auto_ai_cr.config import ToolConfig
from auto_ai_cr.reviewer import (
    _command_environment,
    _ensure_command_available,
    _format_command_report,
    _render_command_template,
    build_prompt,
    parse_issues,
)


def test_render_command_template_does_not_require_escaping_other_braces():
    command = "tool --json '{\"mode\":\"review\"}' --repo {repo}"

    assert _render_command_template(command, {"repo": "/tmp/repo"}) == (
        "tool --json '{\"mode\":\"review\"}' --repo /tmp/repo"
    )


def test_parse_issues_from_auto_ai_cr_json_block():
    output = """
## Review Output

```auto-ai-cr-issues
{
  "issues": [
    {
      "id": "CR-001",
      "severity": "critical",
      "title": "Bug",
      "file": "src/app.py",
      "line": 12,
      "description": "desc",
      "risk": "risk",
      "recommendation": "fix it"
    }
  ]
}
```
"""

    issues = parse_issues(output)

    assert len(issues) == 1
    assert issues[0].id == "CR-001"
    assert issues[0].severity == "critical"
    assert issues[0].file == "src/app.py"
    assert issues[0].line == 12


def test_missing_review_command_fails_before_shell_report():
    try:
        _ensure_command_available("definitely-not-a-real-auto-ai-cr-command review -")
    except ValueError as exc:
        assert "找不到 CR 工具" in str(exc)
        assert "definitely-not-a-real-auto-ai-cr-command" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_absolute_review_command_is_allowed(tmp_path):
    executable = tmp_path / "codex"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")

    _ensure_command_available(f"{executable} review -")


def test_command_environment_prepends_absolute_command_dir(tmp_path):
    bin_dir = tmp_path / "node-bin"
    bin_dir.mkdir()
    executable = bin_dir / "codex"
    executable.write_text("#!/usr/bin/env node\n", encoding="utf-8")

    env = _command_environment(f"{executable} review -", {"PATH": "/usr/bin"})

    assert env["PATH"].split(os.pathsep)[0] == str(bin_dir.resolve())
    assert "/usr/bin" in env["PATH"].split(os.pathsep)


def test_command_environment_keeps_symlink_bin_dir(tmp_path):
    node_root = tmp_path / "node"
    node_bin = node_root / "bin"
    package_bin = node_root / "lib" / "node_modules" / "@openai" / "codex" / "bin"
    node_bin.mkdir(parents=True)
    package_bin.mkdir(parents=True)
    real_codex = package_bin / "codex.js"
    real_codex.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    symlink_codex = node_bin / "codex"
    symlink_codex.symlink_to(real_codex)

    env = _command_environment(f"{symlink_codex} review -", {"PATH": "/usr/bin"})

    assert env["PATH"].split(os.pathsep)[0] == str(node_bin)


def test_command_environment_preserves_windows_path_key(tmp_path, monkeypatch):
    bin_dir = tmp_path / "node-bin"
    bin_dir.mkdir()
    executable = bin_dir / "codex"
    executable.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    monkeypatch.setattr("auto_ai_cr.reviewer._path_env_key", lambda env: "Path")
    monkeypatch.setattr("auto_ai_cr.reviewer.os.pathsep", ";")

    env = _command_environment(str(executable), {"Path": "C:\\Windows"})

    assert "Path" in env
    assert "PATH" not in env
    assert env["Path"].split(";")[0] == str(bin_dir.resolve())


def test_build_prompt_requests_concise_human_report(tmp_path):
    diff = DiffResult(
        scope="latest_commit",
        base_branch="master",
        head_sha="abc123",
        subject="subject",
        diff="diff --git a/app.py b/app.py\n",
        truncated=False,
    )

    prompt = build_prompt(tmp_path, diff)

    assert "# CR 结果" in prompt
    assert "每条最多 4 行" in prompt
    assert "120 行以内" in prompt


def test_command_report_puts_review_before_runtime_details():
    diff = DiffResult(
        scope="latest_commit",
        base_branch="master",
        head_sha="abcdef1234567890",
        subject="tight report",
        diff="",
        truncated=False,
    )
    completed = subprocess.CompletedProcess(
        args="review",
        returncode=0,
        stdout="# CR 结果\n\n## 结论\n- 状态：通过\n",
        stderr="debug noise",
    )

    report = "\n".join(_format_command_report(ToolConfig(type="command", command="codex review -"), diff, completed))

    assert report.index("# CR 结果") < report.index("<summary>运行信息</summary>")
    assert "<summary>工具错误输出</summary>" in report
    assert "## Review Output" not in report

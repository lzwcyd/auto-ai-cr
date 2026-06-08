from auto_ai_cr.config import AppConfig
from auto_ai_cr.fixer import build_fix_prompt, save_fix_prompt
from auto_ai_cr.reviewer import ReviewIssue


def test_build_fix_prompt_limits_agent_to_selected_issues(tmp_path):
    issue = ReviewIssue(
        id="CR-001",
        severity="warning",
        title="Only this one",
        file="src/app.py",
        line=7,
        description="desc",
        risk="risk",
        recommendation="fix selected",
    )

    prompt = build_fix_prompt(tmp_path, [issue], tmp_path / "report.md")

    assert "只修复" in prompt
    assert "CR-001" in prompt
    assert "src/app.py" in prompt
    assert "不要自动提交" in prompt


def test_save_fix_prompt_writes_agent_specific_prompt(tmp_path):
    issue = ReviewIssue(
        id="CR-002",
        severity="critical",
        title="Needs cursor",
        file="src/main.py",
        line=10,
        description="desc",
        risk="risk",
        recommendation="fix it",
    )
    config = AppConfig(fix_tool="cursor")

    result = save_fix_prompt(tmp_path, config, [issue], tmp_path / "report.md")

    assert result.prompt_path.exists()
    assert "CR-002" in result.prompt
    assert "Cursor Agent" in result.prompt
    assert result.prompt_path.read_text(encoding="utf-8") == result.prompt

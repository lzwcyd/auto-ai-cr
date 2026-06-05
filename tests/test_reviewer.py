from auto_ai_cr.reviewer import _render_command_template, parse_issues


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

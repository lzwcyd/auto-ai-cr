from auto_ai_cr.reviewer import _render_command_template


def test_render_command_template_does_not_require_escaping_other_braces():
    command = "tool --json '{\"mode\":\"review\"}' --repo {repo}"

    assert _render_command_template(command, {"repo": "/tmp/repo"}) == (
        "tool --json '{\"mode\":\"review\"}' --repo /tmp/repo"
    )

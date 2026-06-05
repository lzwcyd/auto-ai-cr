from auto_ai_cr.web_ui import _command_status, _ensure_loopback_host


def test_ui_host_must_be_loopback():
    _ensure_loopback_host("127.0.0.1")
    _ensure_loopback_host("localhost")


def test_ui_host_rejects_public_bind_address():
    try:
        _ensure_loopback_host("0.0.0.0")
    except ValueError as exc:
        assert "loopback" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_command_status_reports_missing_command():
    status = _command_status("definitely-not-a-real-auto-ai-cr-command")

    assert status == {"installed": False, "path": ""}

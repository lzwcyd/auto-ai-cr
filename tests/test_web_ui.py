import socket

from auto_ai_cr.web_ui import (
    _candidate_ports,
    _command_status,
    _create_ui_server,
    _ensure_loopback_host,
    _handler,
    _job_status_from_result,
    _merge_recent_reviews,
    _read_report,
    _save_ui_profile,
    _target_entries,
    _target_for_project,
    _targets_from_value,
    _vscode_open_command,
)


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


def test_review_job_status_reflects_skipped_and_failed_runs():
    assert _job_status_from_result({"skipped": True}) == "skipped"
    assert _job_status_from_result({"exitCode": 2}) == "failed"
    assert _job_status_from_result({"exitCode": 0}) == "done"


def test_vscode_open_command_uses_macos_app_launcher(monkeypatch):
    monkeypatch.setattr("auto_ai_cr.web_ui.platform.system", lambda: "Darwin")

    assert _vscode_open_command() == "open -a 'Visual Studio Code' {report}"


def test_target_entries_accepts_multiple_lines_and_commas():
    assert _target_entries("/tmp/a\n/tmp/b, /tmp/c") == ["/tmp/a", "/tmp/b", "/tmp/c"]


def test_targets_from_value_uses_saved_ui_profile(tmp_path, monkeypatch):
    profile = tmp_path / "ui.json"
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.setattr("auto_ai_cr.web_ui.UI_STATE_PATH", profile)
    _save_ui_profile([target], "")

    targets = _targets_from_value(tmp_path, "")

    assert targets == [target.resolve()]


def test_target_for_project_prefers_containing_root(tmp_path):
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    project = root_b / "repo"
    project.mkdir(parents=True)
    root_a.mkdir()

    assert _target_for_project([root_a, root_b], project) == root_b


def test_merge_recent_reviews_keeps_global_latest_first():
    older = {
        "repo": "/repo/a",
        "sha": "aaa",
        "reportPath": "/reports/a.md",
        "finishedAt": "2026-06-08T01:00:00+00:00",
    }
    latest = {
        "repo": "/repo/b",
        "sha": "bbb",
        "reportPath": "/reports/b.md",
        "finishedAt": "2026-06-08T02:00:00+00:00",
    }

    rows = _merge_recent_reviews([older], [latest])

    assert rows[0] == latest
    assert rows[1] == older


def test_read_report_allows_auto_ai_cr_review_files(tmp_path):
    report = tmp_path / ".auto-ai-cr" / "reviews" / "report.md"
    report.parent.mkdir(parents=True)
    report.write_text("# Report\n", encoding="utf-8")

    result = _read_report(str(report))

    assert result["name"] == "report.md"
    assert result["content"] == "# Report\n"


def test_read_report_rejects_non_review_files(tmp_path):
    report = tmp_path / "report.md"
    report.write_text("# Report\n", encoding="utf-8")

    try:
      _read_report(str(report))
    except ValueError as exc:
      assert "auto-ai-cr" in str(exc)
    else:
      raise AssertionError("expected ValueError")


def test_candidate_ports_fall_back_after_requested_port():
    assert _candidate_ports(8765, 3) == [8765, 8766, 8767, 8768]
    assert _candidate_ports(0, 3) == [0]


def test_create_ui_server_uses_next_port_when_requested_port_is_busy(tmp_path):
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    busy_port = busy.getsockname()[1]

    server = _create_ui_server(_handler(tmp_path), "127.0.0.1", busy_port, fallback_count=20)
    try:
        assert server.server_port != busy_port
        assert busy_port < server.server_port <= busy_port + 20
    finally:
        server.server_close()
        busy.close()

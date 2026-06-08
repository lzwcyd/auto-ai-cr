from pathlib import Path

import auto_ai_cr.monitor as monitor
from auto_ai_cr.monitor import (
    expected_trace2_target,
    monitor_label,
    monitor_socket_path,
    recent_reviews,
    record_review_finished,
    record_review_started,
    repo_key,
)


def test_expected_trace2_target_points_to_auto_ai_cr_event_log():
    repo = Path("/tmp/example")

    assert expected_trace2_target(repo) == str(monitor_socket_path(repo))
    assert str(monitor_socket_path(repo)).endswith("trace2-event.jsonl")
    assert ".auto-ai-cr" in monitor_socket_path(repo).parts
    assert "daemon" in monitor_socket_path(repo).parts


def test_monitor_label_is_global_daemon_label():
    repo = Path("/tmp/example")

    assert monitor_label(repo) == "com.auto-ai-cr.daemon"
    assert repo_key(repo) == repo_key(repo)


def test_recent_reviews_returns_recorded_repo_runs(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(monitor, "STATE_PATH", state_path)
    monkeypatch.setattr(monitor, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(monitor, "EVENT_PATH", tmp_path / "trace2-event.jsonl")
    repo = tmp_path / "repo"
    repo.mkdir()

    record_review_started(repo, "abc123", "latest_commit", source="daemon")
    record_review_finished(
        repo,
        "abc123",
        "done",
        report_path=repo / ".auto-ai-cr/reviews/report.md",
        issue_count=2,
        exit_code=0,
    )

    rows = recent_reviews(repo)

    assert len(rows) == 1
    assert rows[0]["status"] == "done"
    assert rows[0]["issueCount"] == 2
    assert rows[0]["source"] == "daemon"

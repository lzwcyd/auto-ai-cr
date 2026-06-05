from pathlib import Path

from auto_ai_cr.monitor import expected_trace2_target, monitor_label, monitor_socket_path, repo_key


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

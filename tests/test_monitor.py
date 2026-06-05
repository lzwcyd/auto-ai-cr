from pathlib import Path

from auto_ai_cr.monitor import expected_trace2_target, monitor_label, monitor_socket_path, repo_key


def test_expected_trace2_target_points_to_auto_ai_cr_socket():
    repo = Path("/tmp/example")

    assert expected_trace2_target(repo) == f"af_unix:stream:{monitor_socket_path(repo)}"
    assert ".auto-ai-cr/daemon" in str(monitor_socket_path(repo))


def test_monitor_label_is_stable_for_repo_path():
    repo = Path("/tmp/example")

    assert monitor_label(repo).startswith("com.auto-ai-cr.example-")
    assert repo_key(repo) == repo_key(repo)

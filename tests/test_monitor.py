from pathlib import Path
import json
import subprocess

import auto_ai_cr.cli as cli
import auto_ai_cr.monitor as monitor
from auto_ai_cr.config import AppConfig
from auto_ai_cr.git_ops import GitError
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


def test_daemon_run_marks_commit_failed_when_diff_collection_fails(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(monitor, "STATE_PATH", state_path)
    monkeypatch.setattr(monitor, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(monitor, "EVENT_PATH", tmp_path / "trace2-event.jsonl")
    repo = tmp_path / "repo"
    repo.mkdir()

    def fail_collect_diff(repo, request):
        raise GitError("bad diff")

    monkeypatch.setattr(cli, "collect_diff", fail_collect_diff)

    try:
        cli._run_once(repo, AppConfig(), commit_sha="abc123")
    except GitError:
        pass

    rows = list(json.loads(state_path.read_text())["processed"].values())
    assert rows[0]["status"] == "failed"
    assert rows[0]["sha"] == "abc123"
    assert rows[0]["source"] == "daemon"
    assert rows[0]["error"] == "bad diff"


def test_trigger_review_marks_failed_when_process_cannot_start(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(monitor, "STATE_PATH", state_path)
    monkeypatch.setattr(monitor, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(monitor, "EVENT_PATH", tmp_path / "trace2-event.jsonl")
    monkeypatch.setattr(monitor, "RUN_OUT_PATH", tmp_path / "run.out.log")
    monkeypatch.setattr(monitor, "RUN_ERR_PATH", tmp_path / "run.err.log")
    repo = tmp_path / "repo"
    repo.mkdir()

    def fail_popen(*args, **kwargs):
        raise OSError("no launcher")

    monkeypatch.setattr(subprocess, "Popen", fail_popen)

    monitor._trigger_review(repo, "abc123")

    rows = list(json.loads(state_path.read_text())["processed"].values())
    assert rows[0]["status"] == "failed"
    assert rows[0]["sha"] == "abc123"
    assert "no launcher" in rows[0]["error"]


def test_trigger_review_records_pending_until_review_finishes(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(monitor, "STATE_PATH", state_path)
    monkeypatch.setattr(monitor, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(monitor, "EVENT_PATH", tmp_path / "trace2-event.jsonl")
    monkeypatch.setattr(monitor, "RUN_OUT_PATH", tmp_path / "run.out.log")
    monkeypatch.setattr(monitor, "RUN_ERR_PATH", tmp_path / "run.err.log")
    repo = tmp_path / "repo"
    repo.mkdir()

    class PopenStub:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(subprocess, "Popen", PopenStub)

    monitor._trigger_review(repo, "abc123")

    state = json.loads(state_path.read_text())
    key = f"{repo.resolve()}|abc123"
    assert state["processed"][key]["status"] == "queued"
    assert state["pendingReviews"][key]["status"] == "dispatching"

    record_review_finished(repo, "abc123", "done")

    state = json.loads(state_path.read_text())
    assert key not in state["pendingReviews"]


def test_resume_pending_reviews_dispatches_stale_items(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(monitor, "STATE_PATH", state_path)
    monkeypatch.setattr(monitor, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(monitor, "EVENT_PATH", tmp_path / "trace2-event.jsonl")
    monkeypatch.setattr(monitor, "RUN_OUT_PATH", tmp_path / "run.out.log")
    monkeypatch.setattr(monitor, "RUN_ERR_PATH", tmp_path / "run.err.log")
    repo = tmp_path / "repo"
    repo.mkdir()
    key = f"{repo.resolve()}|abc123"
    state_path.write_text(
        json.dumps(
            {
                "watchedRepos": [],
                "watchedRoots": [],
                "processed": {
                    key: {
                        "queuedAt": "2026-06-29T00:00:00+00:00",
                        "repo": str(repo.resolve()),
                        "sha": "abc123",
                        "scope": "latest_commit",
                        "source": "daemon",
                        "status": "queued",
                    }
                },
                "pendingReviews": {
                    key: {
                        "queuedAt": "2026-06-29T00:00:00+00:00",
                        "repo": str(repo.resolve()),
                        "sha": "abc123",
                        "scope": "latest_commit",
                        "source": "daemon",
                        "status": "queued",
                        "attempts": 0,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    calls = []

    class PopenStub:
        def __init__(self, command, **kwargs):
            calls.append(command)

    monkeypatch.setattr(subprocess, "Popen", PopenStub)

    monitor._resume_pending_reviews()

    state = json.loads(state_path.read_text())
    assert len(calls) == 1
    assert state["pendingReviews"][key]["status"] == "dispatching"
    assert state["pendingReviews"][key]["attempts"] == 1

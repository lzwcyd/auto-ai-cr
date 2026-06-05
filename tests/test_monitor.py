from pathlib import Path

from auto_ai_cr.monitor import COMMIT_RE, monitor_label, repo_key


def test_commit_event_regex_extracts_repo_and_sha():
    sha = "a" * 40
    line = f'git write op completed op="commit" repo=/tmp/repo new_head={sha}'

    match = COMMIT_RE.search(line)

    assert match
    assert match.group("repo") == "/tmp/repo"
    assert match.group("sha") == sha


def test_monitor_label_is_stable_for_repo_path():
    repo = Path("/tmp/example")

    assert monitor_label(repo).startswith("com.auto-ai-cr.example-")
    assert repo_key(repo) == repo_key(repo)

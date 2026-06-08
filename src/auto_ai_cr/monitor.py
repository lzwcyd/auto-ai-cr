from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import plistlib
import platform
import subprocess
import sys
import time

from .git_ops import find_repo, run_git, try_find_repo


STATE_ROOT = Path.home() / ".auto-ai-cr/daemon"
STATE_PATH = STATE_ROOT / "state.json"
EVENT_PATH = STATE_ROOT / "trace2-event.jsonl"
LAUNCH_AGENT_DIR = Path.home() / "Library/LaunchAgents"
LAUNCH_LABEL = "com.auto-ai-cr.daemon"


@dataclass(frozen=True)
class MonitorStatus:
    installed: bool
    running: bool
    label: str
    plist_path: Path
    state_path: Path
    event_path: Path
    trace2_target: str
    expected_trace2_target: str
    repo_watched: bool
    target_type: str

    def to_mapping(self) -> dict[str, object]:
        return {
            "installed": self.installed,
            "running": self.running,
            "label": self.label,
            "plistPath": str(self.plist_path),
            "launcherPath": str(self.plist_path),
            "launcherKind": _launcher_kind(),
            "statePath": str(self.state_path),
            "socketPath": str(self.event_path),
            "eventPath": str(self.event_path),
            "trace2Target": self.trace2_target,
            "expectedTrace2Target": self.expected_trace2_target,
            "repoWatched": self.repo_watched,
            "targetType": self.target_type,
        }


class Trace2Session:
    def __init__(self) -> None:
        self.worktree: str | None = None
        self.command: str | None = None
        self.completed = False


def run_monitor(repo: Path | None = None, once: bool = False, poll_seconds: float = 2.0) -> int:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    EVENT_PATH.touch(exist_ok=True)
    offset = EVENT_PATH.stat().st_size
    sessions: dict[str, Trace2Session] = {}

    while True:
        offset = _scan_event_file(offset, sessions)
        if once:
            return 0
        time.sleep(poll_seconds)


def install_monitor(repo: Path) -> MonitorStatus:
    target_type, target_path = _resolve_watch_target(repo)
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    _remember_previous_trace2_target()
    _add_watched_target(target_type, target_path)

    _install_launcher()

    run_git(Path.cwd(), ["config", "--global", "trace2.eventtarget", expected_trace2_target()])
    return monitor_status(target_path)


def uninstall_monitor(repo: Path) -> MonitorStatus:
    target_type, target_path = _resolve_watch_target(repo)
    _remove_watched_target(target_type, target_path)
    state = _load_state()
    if not state.get("watchedRepos") and not state.get("watchedRoots"):
        _uninstall_launcher()
        if _trace2_target() == expected_trace2_target():
            previous = str(state.get("previousTrace2Target") or "")
            if previous:
                run_git(Path.cwd(), ["config", "--global", "trace2.eventtarget", previous], check=False)
            else:
                run_git(Path.cwd(), ["config", "--global", "--unset", "trace2.eventtarget"], check=False)
    return monitor_status(target_path)


def monitor_status(repo: Path) -> MonitorStatus:
    target_type, target_path = _resolve_watch_target(repo)
    state = _load_state()
    watched_key = "watchedRepos" if target_type == "repo" else "watchedRoots"
    watched = str(target_path.resolve()) in state.get(watched_key, [])
    return MonitorStatus(
        installed=_is_launcher_installed(),
        running=_is_launcher_running(),
        label=LAUNCH_LABEL,
        plist_path=_launcher_path(),
        state_path=STATE_PATH,
        event_path=EVENT_PATH,
        trace2_target=_trace2_target(),
        expected_trace2_target=expected_trace2_target(),
        repo_watched=watched,
        target_type=target_type,
    )


def expected_trace2_target(repo: Path | None = None) -> str:
    return str(EVENT_PATH)


def monitor_label(repo: Path | None = None) -> str:
    return LAUNCH_LABEL


def monitor_plist_path(repo: Path | None = None) -> Path:
    return _launcher_path()


def monitor_state_path(repo: Path | None = None) -> Path:
    return STATE_PATH


def monitor_socket_path(repo: Path | None = None) -> Path:
    return EVENT_PATH


def repo_key(repo: Path) -> str:
    digest = hashlib.sha1(str(repo.resolve()).encode("utf-8")).hexdigest()[:12]
    return f"{repo.name}-{digest}"


def record_review_started(
    repo: Path,
    sha: str,
    scope: str,
    source: str = "manual",
) -> None:
    state = _load_state()
    processed = state.setdefault("processed", {})
    key = f"{repo.resolve()}|{sha}"
    current = processed.get(key, {}) if isinstance(processed.get(key), dict) else {}
    current.update(
        {
            "repo": str(repo.resolve()),
            "sha": sha,
            "scope": scope,
            "source": source,
            "status": "running",
            "startedAt": _now(),
        }
    )
    processed[key] = current
    _save_state(state)


def record_review_finished(
    repo: Path,
    sha: str,
    status: str,
    report_path: Path | None = None,
    issues_path: Path | None = None,
    issue_count: int | None = None,
    exit_code: int | None = None,
    error: str | None = None,
) -> None:
    state = _load_state()
    processed = state.setdefault("processed", {})
    key = f"{repo.resolve()}|{sha}"
    current = processed.get(key, {}) if isinstance(processed.get(key), dict) else {}
    current.update(
        {
            "repo": str(repo.resolve()),
            "sha": sha,
            "status": status,
            "finishedAt": _now(),
        }
    )
    if report_path is not None:
        current["reportPath"] = str(report_path)
    if issues_path is not None:
        current["issuesPath"] = str(issues_path)
    if issue_count is not None:
        current["issueCount"] = issue_count
    if exit_code is not None:
        current["exitCode"] = exit_code
    if error:
        current["error"] = error
    processed[key] = current
    _save_state(state)


def recent_reviews(target: Path, limit: int = 8) -> list[dict[str, object]]:
    target_type, target_path = _resolve_watch_target(target)
    rows = []
    for value in _processed_review_rows():
        repo_value = value.get("repo")
        if not isinstance(repo_value, str):
            continue
        try:
            repo_path = Path(repo_value).resolve()
        except Exception:
            continue
        if target_type == "repo":
            if repo_path != target_path:
                continue
        else:
            try:
                repo_path.relative_to(target_path)
            except ValueError:
                continue
        rows.append(value)
    rows.sort(key=lambda row: str(row.get("finishedAt") or row.get("startedAt") or row.get("queuedAt") or ""), reverse=True)
    return rows[:limit]


def recent_reviews_global(limit: int = 8) -> list[dict[str, object]]:
    rows = _processed_review_rows()
    rows.sort(key=lambda row: str(row.get("finishedAt") or row.get("startedAt") or row.get("queuedAt") or ""), reverse=True)
    return rows[:limit]


def _processed_review_rows() -> list[dict[str, object]]:
    state = _load_state()
    return [
        value
        for value in state.get("processed", {}).values()
        if isinstance(value, dict)
    ]


def _scan_event_file(offset: int, sessions: dict[str, Trace2Session]) -> int:
    try:
        size = EVENT_PATH.stat().st_size
    except OSError:
        return offset
    if size < offset:
        offset = 0
    if size == offset:
        return offset
    with EVENT_PATH.open("rb") as fp:
        fp.seek(offset)
        for line in fp:
            if line.strip():
                _handle_trace2_line(line.rstrip(b"\n"), sessions)
        return fp.tell()


def _handle_trace2_line(raw: bytes, sessions: dict[str, Trace2Session]) -> None:
    try:
        event = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return

    sid = str(event.get("sid") or "")
    if not sid:
        return
    session = sessions.setdefault(sid, Trace2Session())
    event_name = event.get("event")

    if event_name == "def_repo" and event.get("worktree"):
        session.worktree = str(event["worktree"])
        return
    if event_name == "cmd_name" and event.get("name"):
        session.command = str(event["name"])
        return
    if event_name not in {"exit", "atexit"}:
        return
    if session.completed or session.command != "commit" or int(event.get("code", 1)) != 0:
        sessions.pop(sid, None)
        return
    if not session.worktree:
        sessions.pop(sid, None)
        return

    repo = _watched_repo_for_worktree(Path(session.worktree))
    if repo is None:
        sessions.pop(sid, None)
        return

    session.completed = True
    time.sleep(0.05)
    sha = run_git(repo, ["rev-parse", "HEAD"]).strip()
    _trigger_review(repo, sha)
    sessions.pop(sid, None)


def _watched_repo_for_worktree(worktree: Path) -> Path | None:
    try:
        resolved = worktree.resolve()
    except Exception:
        return None
    state = _load_state()
    for repo in state.get("watchedRepos", []):
        repo_path = Path(repo).resolve()
        if repo_path == resolved:
            return repo_path
    for root in state.get("watchedRoots", []):
        root_path = Path(root).resolve()
        try:
            resolved.relative_to(root_path)
        except ValueError:
            continue
        repo_path = try_find_repo(resolved)
        if repo_path and repo_path == resolved:
            return repo_path
    return None


def _config_root_for_repo(repo: Path) -> Path:
    state = _load_state()
    for root in state.get("watchedRoots", []):
        root_path = Path(root).resolve()
        try:
            repo.resolve().relative_to(root_path)
        except ValueError:
            continue
        return root_path
    return repo


def _trigger_review(repo: Path, sha: str) -> None:
    state = _load_state()
    processed = state.setdefault("processed", {})
    key = f"{repo}|{sha}"
    if key in processed:
        return
    processed[key] = {
        "queuedAt": _now(),
        "repo": str(repo),
        "sha": sha,
        "scope": "latest_commit",
        "source": "daemon",
        "status": "queued",
    }
    _save_state(state)

    env = os.environ.copy()
    if not getattr(sys, "frozen", False):
        env.setdefault("PYTHONPATH", str(_package_src_path()))
    kwargs: dict[str, object] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(
        [
            *_daemon_command(),
            "run",
            "--repo",
            str(repo),
            "--scope",
            "latest_commit",
            "--commit",
            sha,
            "--config-root",
            str(_config_root_for_repo(repo)),
        ],
        cwd=repo,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **kwargs,
    )


def _add_watched_target(target_type: str, path: Path) -> None:
    state = _load_state()
    key = "watchedRepos" if target_type == "repo" else "watchedRoots"
    watched = set(state.setdefault(key, []))
    watched.add(str(path.resolve()))
    state[key] = sorted(watched)
    _save_state(state)


def _remove_watched_target(target_type: str, path: Path) -> None:
    state = _load_state()
    key = "watchedRepos" if target_type == "repo" else "watchedRoots"
    watched = set(state.setdefault(key, []))
    watched.discard(str(path.resolve()))
    state[key] = sorted(watched)
    _save_state(state)


def _remember_previous_trace2_target() -> None:
    state = _load_state()
    current = _trace2_target()
    if current and current != expected_trace2_target() and not state.get("previousTrace2Target"):
        state["previousTrace2Target"] = current
        _save_state(state)


def _load_state() -> dict[str, object]:
    if not STATE_PATH.exists():
        return {"watchedRepos": [], "watchedRoots": [], "processed": {}, "startedAt": _now()}
    try:
        with STATE_PATH.open("r", encoding="utf-8") as fp:
            state = json.load(fp)
    except Exception:
        return {"watchedRepos": [], "watchedRoots": [], "processed": {}, "startedAt": _now()}
    state.setdefault("watchedRepos", [])
    state.setdefault("watchedRoots", [])
    state.setdefault("processed", {})
    return state


def _resolve_watch_target(path: Path) -> tuple[str, Path]:
    path = path.expanduser().resolve()
    repo = try_find_repo(path)
    if repo is not None:
        return "repo", repo
    if path.is_dir():
        return "root", path
    raise ValueError(f"path does not exist: {path}")


def _save_state(state: dict[str, object]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fp:
        json.dump(state, fp, ensure_ascii=False, indent=2, sort_keys=True)
    tmp.replace(STATE_PATH)


def _trace2_target() -> str:
    try:
        return run_git(Path.cwd(), ["config", "--global", "--get", "trace2.eventtarget"], check=False).strip()
    except Exception:
        return ""


def _daemon_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "auto_ai_cr.cli"]


def _package_src_path() -> Path:
    return Path(__file__).resolve().parents[1]


def _install_launcher() -> None:
    system = platform.system()
    if system == "Darwin":
        plist_path = _launcher_path()
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "Label": LAUNCH_LABEL,
            "ProgramArguments": [*_daemon_command(), "monitor"],
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(STATE_ROOT / "daemon.out.log"),
            "StandardErrorPath": str(STATE_ROOT / "daemon.err.log"),
        }
        if not getattr(sys, "frozen", False):
            payload["EnvironmentVariables"] = {"PYTHONPATH": str(_package_src_path())}
        with plist_path.open("wb") as fp:
            plistlib.dump(payload, fp)
        _launchctl(["bootout", _launch_domain(), str(plist_path)], check=False)
        _launchctl(["bootstrap", _launch_domain(), str(plist_path)], check=False)
        _launchctl(["kickstart", "-k", f"{_launch_domain()}/{LAUNCH_LABEL}"], check=False)
        return
    if system == "Linux":
        service_path = _launcher_path()
        service_path.parent.mkdir(parents=True, exist_ok=True)
        command = " ".join(_shell_quote(part) for part in [*_daemon_command(), "monitor"])
        env_line = (
            ""
            if getattr(sys, "frozen", False)
            else f'Environment="PYTHONPATH={_systemd_escape(str(_package_src_path()))}"\n'
        )
        service_path.write_text(
            "[Unit]\n"
            "Description=auto-ai-cr daemon\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"{env_line}"
            f"ExecStart={command}\n"
            "Restart=always\n\n"
            "[Install]\n"
            "WantedBy=default.target\n",
            encoding="utf-8",
        )
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "--user", "enable", "--now", f"{LAUNCH_LABEL}.service"], check=False)
        return
    if system == "Windows":
        task_name = "auto-ai-cr-daemon"
        command = " ".join(_windows_quote(part) for part in [*_daemon_command(), "monitor"])
        subprocess.run(["schtasks", "/Delete", "/TN", task_name, "/F"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        completed = subprocess.run(
            ["schtasks", "/Create", "/TN", task_name, "/SC", "ONLOGON", "/TR", command, "/F"],
            check=False,
        )
        if completed.returncode == 0:
            _launcher_path().write_text(command + "\n", encoding="utf-8")
            subprocess.run(["schtasks", "/Run", "/TN", task_name], check=False)


def _uninstall_launcher() -> None:
    system = platform.system()
    if system == "Darwin":
        plist_path = _launcher_path()
        if plist_path.exists():
            _launchctl(["bootout", _launch_domain(), str(plist_path)], check=False)
            plist_path.unlink()
        return
    if system == "Linux":
        subprocess.run(["systemctl", "--user", "disable", "--now", f"{LAUNCH_LABEL}.service"], check=False)
        service_path = _launcher_path()
        if service_path.exists():
            service_path.unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        return
    if system == "Windows":
        subprocess.run(["schtasks", "/Delete", "/TN", "auto-ai-cr-daemon", "/F"], check=False)
        marker = _launcher_path()
        if marker.exists():
            marker.unlink()


def _launcher_path() -> Path:
    system = platform.system()
    if system == "Darwin":
        return LAUNCH_AGENT_DIR / f"{LAUNCH_LABEL}.plist"
    if system == "Linux":
        return Path.home() / ".config/systemd/user" / f"{LAUNCH_LABEL}.service"
    if system == "Windows":
        return STATE_ROOT / "auto-ai-cr-daemon.task"
    return STATE_ROOT / "auto-ai-cr-daemon"


def _launcher_kind() -> str:
    system = platform.system()
    if system == "Darwin":
        return "LaunchAgent"
    if system == "Linux":
        return "systemd user service"
    if system == "Windows":
        return "Windows scheduled task"
    return "process launcher"


def _is_launcher_installed() -> bool:
    system = platform.system()
    if system == "Windows":
        completed = subprocess.run(
            ["schtasks", "/Query", "/TN", "auto-ai-cr-daemon"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return completed.returncode == 0 or _launcher_path().exists()
    return _launcher_path().exists()


def _is_launcher_running() -> bool:
    system = platform.system()
    if system == "Darwin":
        return _is_launch_agent_running(LAUNCH_LABEL)
    if system == "Linux":
        completed = subprocess.run(["systemctl", "--user", "is-active", "--quiet", f"{LAUNCH_LABEL}.service"], check=False)
        return completed.returncode == 0
    if system == "Windows":
        completed = subprocess.run(["schtasks", "/Query", "/TN", "auto-ai-cr-daemon"], check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        return completed.returncode == 0 and "Running" in completed.stdout
    return False


def _is_launch_agent_running(label: str) -> bool:
    output = _launchctl(["print", f"{_launch_domain()}/{label}"], check=False)
    return "could not find service" not in output.lower() and "state = running" in output.lower()


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _systemd_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _windows_quote(value: str) -> str:
    return '"' + value.replace('"', r'\"') + '"'


def _launchctl(args: list[str], check: bool) -> str:
    completed = subprocess.run(
        ["launchctl", *args],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(completed.stdout.strip())
    return completed.stdout


def _launch_domain() -> str:
    return f"gui/{os.getuid()}"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()

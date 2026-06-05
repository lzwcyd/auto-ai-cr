from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import plistlib
import socket
import subprocess
import sys
import threading
import time

from .git_ops import find_repo, run_git


STATE_ROOT = Path.home() / ".auto-ai-cr/daemon"
STATE_PATH = STATE_ROOT / "state.json"
SOCKET_PATH = STATE_ROOT / "trace2.sock"
LAUNCH_AGENT_DIR = Path.home() / "Library/LaunchAgents"
LAUNCH_LABEL = "com.auto-ai-cr.daemon"


@dataclass(frozen=True)
class MonitorStatus:
    installed: bool
    running: bool
    label: str
    plist_path: Path
    state_path: Path
    socket_path: Path
    trace2_target: str
    expected_trace2_target: str
    repo_watched: bool

    def to_mapping(self) -> dict[str, object]:
        return {
            "installed": self.installed,
            "running": self.running,
            "label": self.label,
            "plistPath": str(self.plist_path),
            "statePath": str(self.state_path),
            "socketPath": str(self.socket_path),
            "trace2Target": self.trace2_target,
            "expectedTrace2Target": self.expected_trace2_target,
            "repoWatched": self.repo_watched,
        }


class Trace2Session:
    def __init__(self) -> None:
        self.worktree: str | None = None
        self.command: str | None = None
        self.completed = False


def run_monitor(repo: Path | None = None, once: bool = False, poll_seconds: float = 2.0) -> int:
    if once:
        return 0

    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()

    sessions: dict[str, Trace2Session] = {}
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(SOCKET_PATH))
    server.listen(100)

    try:
        while True:
            conn, _ = server.accept()
            thread = threading.Thread(
                target=_handle_connection,
                args=(conn, sessions),
                daemon=True,
            )
            thread.start()
    finally:
        server.close()
        if SOCKET_PATH.exists():
            SOCKET_PATH.unlink()


def install_monitor(repo: Path) -> MonitorStatus:
    repo = find_repo(repo)
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    _remember_previous_trace2_target()
    _add_watched_repo(repo)

    plist_path = monitor_plist_path(repo)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": LAUNCH_LABEL,
        "ProgramArguments": [
            sys.executable,
            "-m",
            "auto_ai_cr.cli",
            "monitor",
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(STATE_ROOT / "daemon.out.log"),
        "StandardErrorPath": str(STATE_ROOT / "daemon.err.log"),
        "EnvironmentVariables": {"PYTHONPATH": str(_package_src_path())},
    }
    with plist_path.open("wb") as fp:
        plistlib.dump(payload, fp)

    run_git(repo, ["config", "--global", "trace2.eventtarget", expected_trace2_target()])
    _launchctl(["bootout", _launch_domain(), str(plist_path)], check=False)
    _launchctl(["bootstrap", _launch_domain(), str(plist_path)], check=False)
    _launchctl(["kickstart", "-k", f"{_launch_domain()}/{LAUNCH_LABEL}"], check=False)
    return monitor_status(repo)


def uninstall_monitor(repo: Path) -> MonitorStatus:
    repo = find_repo(repo)
    _remove_watched_repo(repo)
    state = _load_state()
    if not state.get("watchedRepos"):
        plist_path = monitor_plist_path(repo)
        if plist_path.exists():
            _launchctl(["bootout", _launch_domain(), str(plist_path)], check=False)
            plist_path.unlink()
        if _trace2_target() == expected_trace2_target():
            previous = str(state.get("previousTrace2Target") or "")
            if previous:
                run_git(repo, ["config", "--global", "trace2.eventtarget", previous], check=False)
            else:
                run_git(repo, ["config", "--global", "--unset", "trace2.eventtarget"], check=False)
        if SOCKET_PATH.exists():
            SOCKET_PATH.unlink()
    return monitor_status(repo)


def monitor_status(repo: Path) -> MonitorStatus:
    repo = find_repo(repo)
    state = _load_state()
    watched = str(repo.resolve()) in state.get("watchedRepos", [])
    return MonitorStatus(
        installed=monitor_plist_path(repo).exists(),
        running=_is_launch_agent_running(LAUNCH_LABEL),
        label=LAUNCH_LABEL,
        plist_path=monitor_plist_path(repo),
        state_path=STATE_PATH,
        socket_path=SOCKET_PATH,
        trace2_target=_trace2_target(),
        expected_trace2_target=expected_trace2_target(),
        repo_watched=watched,
    )


def expected_trace2_target(repo: Path | None = None) -> str:
    return f"af_unix:stream:{SOCKET_PATH}"


def monitor_label(repo: Path | None = None) -> str:
    return LAUNCH_LABEL


def monitor_plist_path(repo: Path | None = None) -> Path:
    return LAUNCH_AGENT_DIR / f"{LAUNCH_LABEL}.plist"


def monitor_state_path(repo: Path | None = None) -> Path:
    return STATE_PATH


def monitor_socket_path(repo: Path | None = None) -> Path:
    return SOCKET_PATH


def repo_key(repo: Path) -> str:
    digest = hashlib.sha1(str(repo.resolve()).encode("utf-8")).hexdigest()[:12]
    return f"{repo.name}-{digest}"


def _handle_connection(conn: socket.socket, sessions: dict[str, Trace2Session]) -> None:
    with conn:
        buffer = b""
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                _handle_trace2_line(line, sessions)
        if buffer.strip():
            _handle_trace2_line(buffer, sessions)


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
        return
    if not session.worktree:
        return

    repo = _watched_repo_for_worktree(Path(session.worktree))
    if repo is None:
        return

    session.completed = True
    time.sleep(0.05)
    sha = run_git(repo, ["rev-parse", "HEAD"]).strip()
    _trigger_review(repo, sha)


def _watched_repo_for_worktree(worktree: Path) -> Path | None:
    try:
        resolved = worktree.resolve()
    except Exception:
        return None
    for repo in _load_state().get("watchedRepos", []):
        repo_path = Path(repo).resolve()
        if repo_path == resolved:
            return repo_path
    return None


def _trigger_review(repo: Path, sha: str) -> None:
    state = _load_state()
    processed = state.setdefault("processed", {})
    key = f"{repo}|{sha}"
    if key in processed:
        return
    processed[key] = {"queuedAt": _now(), "repo": str(repo), "sha": sha}
    _save_state(state)

    env = os.environ.copy()
    env.setdefault("PYTHONPATH", str(_package_src_path()))
    subprocess.Popen(
        [
            sys.executable,
            "-m",
            "auto_ai_cr.cli",
            "run",
            "--repo",
            str(repo),
            "--scope",
            "latest_commit",
            "--commit",
            sha,
        ],
        cwd=repo,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _add_watched_repo(repo: Path) -> None:
    state = _load_state()
    watched = set(state.setdefault("watchedRepos", []))
    watched.add(str(repo.resolve()))
    state["watchedRepos"] = sorted(watched)
    _save_state(state)


def _remove_watched_repo(repo: Path) -> None:
    state = _load_state()
    watched = set(state.setdefault("watchedRepos", []))
    watched.discard(str(repo.resolve()))
    state["watchedRepos"] = sorted(watched)
    _save_state(state)


def _remember_previous_trace2_target() -> None:
    state = _load_state()
    current = _trace2_target()
    if current and current != expected_trace2_target() and not state.get("previousTrace2Target"):
        state["previousTrace2Target"] = current
        _save_state(state)


def _load_state() -> dict[str, object]:
    if not STATE_PATH.exists():
        return {"watchedRepos": [], "processed": {}, "startedAt": _now()}
    try:
        with STATE_PATH.open("r", encoding="utf-8") as fp:
            state = json.load(fp)
    except Exception:
        return {"watchedRepos": [], "processed": {}, "startedAt": _now()}
    state.setdefault("watchedRepos", [])
    state.setdefault("processed", {})
    return state


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


def _package_src_path() -> Path:
    return Path(__file__).resolve().parents[1]


def _is_launch_agent_running(label: str) -> bool:
    output = _launchctl(["print", f"{_launch_domain()}/{label}"], check=False)
    return "could not find service" not in output.lower() and "state = running" in output.lower()


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

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import glob
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import time

from .git_ops import find_repo, run_git


GIT_AI_LOG_DIR = Path.home() / ".git-ai/internal/daemon/logs"
STATE_ROOT = Path.home() / ".auto-ai-cr/monitor"
LAUNCH_AGENT_DIR = Path.home() / "Library/LaunchAgents"
COMMIT_RE = re.compile(
    r'git write op completed op="commit" repo=(?P<repo>\S+) new_head=(?P<sha>[0-9a-f]{40})'
)


@dataclass(frozen=True)
class MonitorStatus:
    installed: bool
    running: bool
    label: str
    plist_path: Path
    state_path: Path
    trace2_target: str
    git_ai_log_dir: Path

    def to_mapping(self) -> dict[str, object]:
        return {
            "installed": self.installed,
            "running": self.running,
            "label": self.label,
            "plistPath": str(self.plist_path),
            "statePath": str(self.state_path),
            "trace2Target": self.trace2_target,
            "gitAiLogDir": str(self.git_ai_log_dir),
        }


def run_monitor(repo: Path, once: bool = False, poll_seconds: float = 2.0) -> int:
    repo = find_repo(repo)
    state_path = monitor_state_path(repo)
    state = _load_state(state_path)
    first_scan = not bool(state.get("offsets"))

    while True:
        scan_once(repo, state_path, first_scan)
        first_scan = False
        if once:
            return 0
        time.sleep(poll_seconds)


def scan_once(repo: Path, state_path: Path | None = None, first_scan: bool = False) -> None:
    repo = find_repo(repo)
    state_path = state_path or monitor_state_path(repo)
    state = _load_state(state_path)
    for path in sorted(glob.glob(str(GIT_AI_LOG_DIR / "*.log"))):
        _scan_file(repo, Path(path), state, first_scan)
    _save_state(state_path, state)


def install_monitor(repo: Path) -> MonitorStatus:
    repo = find_repo(repo)
    plist_path = monitor_plist_path(repo)
    label = monitor_label(repo)
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    args = [
        sys.executable,
        "-m",
        "auto_ai_cr.cli",
        "monitor",
        "--repo",
        str(repo),
    ]
    env = {}
    src_path = repo / "src"
    if src_path.exists():
        env["PYTHONPATH"] = str(src_path)

    payload = {
        "Label": label,
        "ProgramArguments": args,
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(STATE_ROOT / f"{repo_key(repo)}.out.log"),
        "StandardErrorPath": str(STATE_ROOT / f"{repo_key(repo)}.err.log"),
    }
    if env:
        payload["EnvironmentVariables"] = env

    with plist_path.open("wb") as fp:
        plistlib.dump(payload, fp)

    _launchctl(["bootout", _launch_domain(), str(plist_path)], check=False)
    _launchctl(["bootstrap", _launch_domain(), str(plist_path)], check=False)
    _launchctl(["kickstart", "-k", f"{_launch_domain()}/{label}"], check=False)
    return monitor_status(repo)


def uninstall_monitor(repo: Path) -> MonitorStatus:
    repo = find_repo(repo)
    plist_path = monitor_plist_path(repo)
    if plist_path.exists():
        _launchctl(["bootout", _launch_domain(), str(plist_path)], check=False)
        plist_path.unlink()
    return monitor_status(repo)


def monitor_status(repo: Path) -> MonitorStatus:
    repo = find_repo(repo)
    label = monitor_label(repo)
    plist_path = monitor_plist_path(repo)
    return MonitorStatus(
        installed=plist_path.exists(),
        running=_is_launch_agent_running(label),
        label=label,
        plist_path=plist_path,
        state_path=monitor_state_path(repo),
        trace2_target=_trace2_target(repo),
        git_ai_log_dir=GIT_AI_LOG_DIR,
    )


def monitor_label(repo: Path) -> str:
    return f"com.auto-ai-cr.{repo_key(repo)}"


def monitor_plist_path(repo: Path) -> Path:
    return LAUNCH_AGENT_DIR / f"{monitor_label(repo)}.plist"


def monitor_state_path(repo: Path) -> Path:
    return STATE_ROOT / f"{repo_key(repo)}.json"


def repo_key(repo: Path) -> str:
    digest = hashlib.sha1(str(repo.resolve()).encode("utf-8")).hexdigest()[:12]
    return f"{repo.name}-{digest}"


def _scan_file(repo: Path, log_path: Path, state: dict[str, object], first_scan: bool) -> None:
    try:
        size = log_path.stat().st_size
    except OSError:
        return

    offsets = state.setdefault("offsets", {})
    processed = state.setdefault("processed", {})
    key = str(log_path)
    if key not in offsets:
        offsets[key] = size if first_scan else 0
        return

    offset = int(offsets.get(key, 0))
    if size < offset:
        offset = 0
    if size == offset:
        return

    with log_path.open("r", encoding="utf-8", errors="replace") as fp:
        fp.seek(offset)
        for line in fp:
            match = COMMIT_RE.search(line)
            if not match:
                continue
            event_repo = Path(match.group("repo")).resolve()
            sha = match.group("sha")
            if event_repo == repo.resolve():
                _trigger_review(repo, sha, processed)
        offsets[key] = fp.tell()


def _trigger_review(repo: Path, sha: str, processed: dict[str, object]) -> None:
    key = f"{repo}|{sha}"
    if key in processed:
        return
    processed[key] = {"queuedAt": _now(), "repo": str(repo), "sha": sha}
    env = os.environ.copy()
    src_path = repo / "src"
    if src_path.exists():
        env["PYTHONPATH"] = str(src_path)
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


def _load_state(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"offsets": {}, "processed": {}, "startedAt": _now()}
    try:
        with path.open("r", encoding="utf-8") as fp:
            state = json.load(fp)
    except Exception:
        return {"offsets": {}, "processed": {}, "startedAt": _now()}
    state.setdefault("offsets", {})
    state.setdefault("processed", {})
    return state


def _save_state(path: Path, state: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fp:
        json.dump(state, fp, ensure_ascii=False, indent=2, sort_keys=True)
    tmp.replace(path)


def _trace2_target(repo: Path) -> str:
    try:
        return run_git(repo, ["config", "--global", "--get", "trace2.eventtarget"], check=False).strip()
    except Exception:
        return ""


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

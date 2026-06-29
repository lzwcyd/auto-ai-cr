from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Callable
from urllib.request import Request, urlopen

from . import __version__


REPO = "lzwcyd/auto-ai-cr"
STATE_ROOT = Path.home() / ".auto-ai-cr"
UPDATE_LOCK = STATE_ROOT / "update.lock"
UPDATE_LOCK_INFO = UPDATE_LOCK / "owner.json"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPO}/releases/latest"
INSTALL_SH_URL = f"https://github.com/{REPO}/releases/latest/download/install.sh"
INSTALL_PS1_URL = f"https://github.com/{REPO}/releases/latest/download/install.ps1"
STALE_UPDATE_LOCK_SECONDS = 15 * 60

ProgressCallback = Callable[[str], None]


@dataclass(frozen=True)
class UpdateResult:
    checked: bool
    current_version: str
    latest_version: str | None = None
    updated: bool = False
    skipped: bool = False
    message: str = ""
    error: str | None = None


def ensure_latest_before_review(progress: ProgressCallback | None = None) -> UpdateResult:
    if _auto_update_disabled():
        return UpdateResult(
            checked=False,
            current_version=__version__,
            skipped=True,
            message="auto update disabled",
        )
    if _is_source_run() and os.environ.get("AUTO_AI_CR_UPDATE_IN_SOURCE") != "1":
        return UpdateResult(
            checked=False,
            current_version=__version__,
            skipped=True,
            message="source checkout run; update check skipped",
        )

    progress = progress or (lambda message: None)
    progress("检查 auto-ai-cr 更新")
    try:
        latest = latest_version()
    except Exception as exc:
        return UpdateResult(
            checked=False,
            current_version=__version__,
            error=str(exc),
            message=f"检查更新失败，继续 CR：{exc}",
        )

    if not is_newer_version(latest, __version__):
        return UpdateResult(
            checked=True,
            current_version=__version__,
            latest_version=latest,
            skipped=True,
            message=f"已是最新版本 {__version__}",
        )

    progress(f"发现新版本 {latest}，正在更新")
    try:
        with _update_lock():
            installed = installed_version()
            if installed and not is_newer_version(latest, installed):
                return UpdateResult(
                    checked=True,
                    current_version=__version__,
                    latest_version=latest,
                    skipped=True,
                    message=f"已由其它进程更新到 {installed}，继续 CR",
                )
            install_latest()
    except Exception as exc:
        return UpdateResult(
            checked=True,
            current_version=__version__,
            latest_version=latest,
            error=str(exc),
            message=f"更新到 {latest} 失败，继续 CR：{exc}",
        )

    return UpdateResult(
        checked=True,
        current_version=__version__,
        latest_version=latest,
        updated=True,
        message=f"已更新到 {latest}，继续 CR",
    )


def latest_version(timeout_seconds: float = 3.0) -> str:
    request = Request(
        os.environ.get("AUTO_AI_CR_LATEST_RELEASE_API", LATEST_RELEASE_API),
        headers={"Accept": "application/vnd.github+json", "User-Agent": f"auto-ai-cr/{__version__}"},
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        payload = json.loads(response.read().decode("utf-8"))
    tag = str(payload.get("tag_name") or payload.get("name") or "").strip()
    if not tag:
        raise ValueError("latest release response did not include tag_name")
    return normalize_version(tag)


def install_latest(timeout_seconds: float = 180.0) -> None:
    with tempfile.TemporaryDirectory(prefix="auto-ai-cr-update-") as tmp:
        tmp_path = Path(tmp)
        env = os.environ.copy()
        env.setdefault("AUTO_AI_CR_VERSION", "latest")
        env["AUTO_AI_CR_RESTART_DAEMON"] = "0"
        if platform.system() == "Windows":
            script = tmp_path / "install.ps1"
            _download(INSTALL_PS1_URL, script)
            powershell = shutil.which("powershell") or shutil.which("pwsh")
            if powershell is None:
                raise RuntimeError("PowerShell is required to update auto-ai-cr on Windows")
            command = [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
        else:
            script = tmp_path / "install.sh"
            _download(INSTALL_SH_URL, script)
            command = ["bash", str(script)]
        completed = subprocess.run(
            command,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout_seconds,
        )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(detail or f"installer exited with code {completed.returncode}")


def installed_version(timeout_seconds: float = 3.0) -> str | None:
    executable = _installed_executable()
    if not executable:
        return None
    completed = subprocess.run(
        [executable, "--version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=timeout_seconds,
    )
    if completed.returncode != 0:
        return None
    match = re.search(r"(\d+(?:\.\d+){1,3}(?:[-+][^\s]+)?)", completed.stdout)
    return normalize_version(match.group(1)) if match else None


def is_newer_version(candidate: str, current: str) -> bool:
    return _version_key(candidate) > _version_key(current)


def normalize_version(value: str) -> str:
    return value.strip().lstrip("vV")


def _version_key(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for token in normalize_version(value).split("."):
        match = re.match(r"\d+", token)
        parts.append(int(match.group(0)) if match else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def _download(url: str, output: Path, timeout_seconds: float = 30.0) -> None:
    request = Request(url, headers={"User-Agent": f"auto-ai-cr/{__version__}"})
    with urlopen(request, timeout=timeout_seconds) as response:
        output.write_bytes(response.read())


def _auto_update_disabled() -> bool:
    return os.environ.get("AUTO_AI_CR_AUTO_UPDATE", "1") == "0"


def _is_source_run() -> bool:
    return not getattr(sys, "frozen", False)


def _installed_executable() -> str | None:
    if getattr(sys, "frozen", False):
        return sys.executable
    return shutil.which("auto-ai-cr")


class _update_lock:
    def __init__(self, timeout_seconds: float = 120.0, stale_seconds: float = STALE_UPDATE_LOCK_SECONDS) -> None:
        self.timeout_seconds = timeout_seconds
        self.stale_seconds = stale_seconds
        self.acquired = False

    def __enter__(self) -> "_update_lock":
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + self.timeout_seconds
        while True:
            try:
                UPDATE_LOCK.mkdir()
                self.acquired = True
                self._write_owner()
                return self
            except FileExistsError:
                if self._remove_stale_lock():
                    continue
                if time.time() > deadline:
                    raise TimeoutError("timed out waiting for another auto-ai-cr update")
                time.sleep(0.5)

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.acquired:
            try:
                UPDATE_LOCK_INFO.unlink(missing_ok=True)
                UPDATE_LOCK.rmdir()
            except OSError:
                pass

    def _write_owner(self) -> None:
        payload = {
            "pid": os.getpid(),
            "createdAt": time.time(),
            "version": __version__,
        }
        UPDATE_LOCK_INFO.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _remove_stale_lock(self) -> bool:
        try:
            created_at = json.loads(UPDATE_LOCK_INFO.read_text(encoding="utf-8")).get("createdAt")
            age = time.time() - float(created_at)
        except Exception:
            try:
                age = time.time() - UPDATE_LOCK.stat().st_mtime
            except OSError:
                return True
        if age < self.stale_seconds:
            return False
        try:
            UPDATE_LOCK_INFO.unlink(missing_ok=True)
            UPDATE_LOCK.rmdir()
            return True
        except OSError:
            return False

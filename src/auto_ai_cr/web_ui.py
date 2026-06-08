from __future__ import annotations

import errno
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import platform
import shutil
import threading
import time
from urllib.parse import parse_qs, urlparse
import uuid
import webbrowser

from .config import (
    CLAUDE_REVIEW_COMMAND,
    CLAUDE_FIX_COMMAND,
    CODEX_REVIEW_COMMAND,
    CODEX_FIX_COMMAND,
    CURSOR_REVIEW_COMMAND,
    CURSOR_FIX_COMMAND,
    AppConfig,
    DEFAULT_REPORTS_DIR,
    load_config,
    write_config,
)
from .fixer import issue_from_mapping, run_fix, save_fix_prompt
from .git_ops import (
    DiffRequest,
    GitError,
    collect_diff,
    current_branch,
    find_repo,
    head_sha,
    run_git,
    try_find_repo,
)
from .monitor import (
    install_monitor,
    monitor_status,
    recent_reviews,
    recent_reviews_global,
    record_review_finished,
    record_review_started,
    uninstall_monitor,
)
from .opener import open_report
from .reviewer import run_review


DEFAULT_PORT = 8765
UI_STATE_PATH = Path.home() / ".auto-ai-cr" / "ui.json"
_JOBS: dict[str, dict[str, object]] = {}
_JOBS_LOCK = threading.Lock()


def _vscode_open_command() -> str:
    if platform.system() == "Darwin":
        return "open -a 'Visual Studio Code' {report}"
    return "code {report}"


def serve_ui(
    repo: Path,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    open_browser: bool = False,
) -> None:
    _ensure_loopback_host(host)
    handler = _handler(repo.expanduser().resolve())
    server = _create_ui_server(handler, host, port)
    url = f"http://{host}:{server.server_port}"
    if port != 0 and server.server_port != port:
        print(f"auto-ai-cr ui: port {port} is busy; using {server.server_port}")
    print(f"auto-ai-cr ui: {url}")
    if open_browser:
        webbrowser.open(url)
    server.serve_forever()


def _create_ui_server(
    handler: type[BaseHTTPRequestHandler],
    host: str,
    port: int,
    fallback_count: int = 20,
) -> ThreadingHTTPServer:
    last_error: OSError | None = None
    for candidate in _candidate_ports(port, fallback_count):
        try:
            return ThreadingHTTPServer((host, candidate), handler)
        except OSError as exc:
            if not _is_port_unavailable_error(exc):
                raise
            last_error = exc
    tried = ", ".join(str(candidate) for candidate in _candidate_ports(port, fallback_count))
    detail = f"; last error: {last_error}" if last_error else ""
    raise ValueError(f"UI port is already in use. Tried: {tried}{detail}")


def _candidate_ports(port: int, fallback_count: int) -> list[int]:
    if port == 0:
        return [0]
    return [port + offset for offset in range(max(1, fallback_count + 1))]


def _is_port_unavailable_error(exc: OSError) -> bool:
    windows_error = getattr(exc, "winerror", None)
    return exc.errno in {errno.EADDRINUSE, errno.EACCES} or windows_error in {10013, 10048}


def _handler(default_repo: Path) -> type[BaseHTTPRequestHandler]:
    class UIHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._html(HTML)
                return
            if parsed.path == "/api/state":
                params = parse_qs(parsed.query)
                targets = _targets_from_value(default_repo, params.get("repo", [""])[0])
                project = params.get("project", [""])[0] or str(_load_ui_profile().get("selectedProject") or "")
                self._json(_state(targets, project))
                return
            if parsed.path == "/api/job":
                try:
                    params = parse_qs(parsed.query)
                    job_id = params.get("id", [""])[0]
                    self._json({"ok": True, "job": _job_snapshot(job_id)})
                except Exception as exc:
                    self._json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            if parsed.path == "/api/report":
                try:
                    params = parse_qs(parsed.query)
                    report_path = params.get("path", [""])[0]
                    self._json({"ok": True, **_read_report(report_path)})
                except Exception as exc:
                    self._json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            try:
                data = self._read_json()
                targets = _targets_from_payload(default_repo, data)
                if self.path == "/api/config":
                    config = AppConfig.from_mapping(data["config"])
                    _write_config_all(targets, config)
                    _save_ui_profile(targets, str(data.get("project") or ""))
                    self._json({"ok": True, "state": _state(targets, str(data.get("project") or ""))})
                    return
                if self.path == "/api/review":
                    config = AppConfig.from_mapping(data["config"])
                    _write_config_all(targets, config)
                    _save_ui_profile(targets, str(data.get("project") or ""))
                    review_repo = _review_repo(targets, str(data.get("project") or ""))
                    result = _run_once(review_repo, config)
                    self._json({"ok": True, **result, "state": _state(targets, str(review_repo))})
                    return
                if self.path == "/api/review/start":
                    config = AppConfig.from_mapping(data["config"])
                    _write_config_all(targets, config)
                    _save_ui_profile(targets, str(data.get("project") or ""))
                    review_repo = _review_repo(targets, str(data.get("project") or ""))
                    job = _start_review_job(targets, review_repo, config)
                    self._json({"ok": True, "job": job, "state": _state(targets, str(review_repo))})
                    return
                if self.path == "/api/fix":
                    config = AppConfig.from_mapping(data["config"])
                    _write_config_all(targets, config)
                    _save_ui_profile(targets, str(data.get("project") or ""))
                    review_repo = _review_repo(targets, str(data.get("project") or ""))
                    issues = [
                        issue_from_mapping(issue)
                        for issue in data.get("issues", [])
                        if isinstance(issue, dict)
                    ]
                    report_path = str(data.get("reportPath") or "")
                    result = _run_fix(review_repo, config, issues, report_path)
                    self._json({"ok": True, **result, "state": _state(targets, str(review_repo))})
                    return
                if self.path == "/api/fix-prompt":
                    config = AppConfig.from_mapping(data["config"])
                    _write_config_all(targets, config)
                    _save_ui_profile(targets, str(data.get("project") or ""))
                    review_repo = _review_repo(targets, str(data.get("project") or ""))
                    issues = [
                        issue_from_mapping(issue)
                        for issue in data.get("issues", [])
                        if isinstance(issue, dict)
                    ]
                    report_path = str(data.get("reportPath") or "")
                    result = _generate_fix_prompt(review_repo, config, issues, report_path)
                    self._json({"ok": True, **result, "state": _state(targets, str(review_repo))})
                    return
                if self.path == "/api/report/open":
                    config = AppConfig.from_mapping(data["config"])
                    report_path = _validated_report_path(str(data.get("reportPath") or ""))
                    open_report(report_path, config.report_open_command)
                    self._json({"ok": True, "message": f"已打开报告: {report_path}"})
                    return
                if self.path in {"/api/monitor", "/api/hook"}:
                    config = AppConfig.from_mapping(data["config"])
                    _write_config_all(targets, config)
                    _save_ui_profile(targets, str(data.get("project") or ""))
                    statuses = [install_monitor(target) for target in targets]
                    self._json(
                        {
                            "ok": True,
                            "message": f"auto-ai-cr daemon 已启用：{len(statuses)} 个目录",
                            "monitor": statuses[0].to_mapping(),
                            "state": _state(targets, str(data.get("project") or "")),
                        }
                    )
                    return
                if self.path == "/api/monitor/stop":
                    _save_ui_profile(targets, str(data.get("project") or ""))
                    statuses = [uninstall_monitor(target) for target in targets]
                    self._json(
                        {
                            "ok": True,
                            "message": f"auto-ai-cr daemon 已停用：{len(statuses)} 个目录",
                            "monitor": statuses[0].to_mapping(),
                            "state": _state(targets, str(data.get("project") or "")),
                        }
                    )
                    return
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def log_message(self, format: str, *args: object) -> None:
            return

        def _read_json(self) -> dict[str, object]:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            return json.loads(raw or "{}")

        def _html(self, body: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _json(
            self, payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK
        ) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return UIHandler


def _ensure_loopback_host(host: str) -> None:
    allowed = {"127.0.0.1", "localhost", "::1"}
    if host not in allowed:
        raise ValueError("UI server only supports loopback hosts: 127.0.0.1, localhost, ::1")


def _targets_from_payload(default_repo: Path, data: dict[str, object]) -> list[Path]:
    value = str(data.get("repo") or data.get("targets") or "")
    return _targets_from_value(default_repo, value)


def _targets_from_value(default_repo: Path, value: str) -> list[Path]:
    entries = _target_entries(value)
    if not entries:
        entries = _load_ui_profile_targets()
    if not entries:
        entries = [str(default_repo)]
    targets = [_target_from_value(default_repo, entry) for entry in entries]
    deduped: list[Path] = []
    seen = set()
    for target in targets:
        key = str(target)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(target)
    return deduped


def _target_entries(value: str) -> list[str]:
    normalized = value.replace(",", "\n").replace(";", "\n")
    return [line.strip() for line in normalized.splitlines() if line.strip()]


def _target_from_value(default_repo: Path, value: str) -> Path:
    target_value = value or str(default_repo)
    target = Path(target_value).expanduser().resolve()
    if not target.exists():
        raise ValueError(f"path does not exist: {target}")
    repo = try_find_repo(target)
    if repo is not None:
        return repo
    if target.is_dir():
        return target
    raise ValueError(f"path is not a git repository or directory: {target}")


def _state(targets: Path | list[Path], selected_project: str = "") -> dict[str, object]:
    target_list = targets if isinstance(targets, list) else [targets]
    target_list = [_target_from_value(target, str(target)) for target in target_list]
    projects = _discover_all_projects(target_list)
    selected_repo = _select_project(target_list[0], projects, selected_project)
    config_target = _target_for_project(target_list, selected_repo) if selected_repo else target_list[0]
    config = load_config(config_target)
    monitor = monitor_status(config_target)
    return {
        "repo": "\n".join(str(path) for path in target_list),
        "targets": [str(path) for path in target_list],
        "targetType": "multi" if len(target_list) > 1 else ("repo" if try_find_repo(target_list[0]) == target_list[0] else "folder"),
        "projects": [
            {
                "path": str(path),
                "name": path.name,
                "target": str(_target_for_project(target_list, path) or target_list[0]),
            }
            for path in projects
        ],
        "selectedProject": str(selected_repo) if selected_repo else "",
        "config": config.to_mapping(),
        "git": {
            "branch": _safe(lambda: current_branch(selected_repo), "unknown") if selected_repo else "-",
            "head": _safe(lambda: head_sha(selected_repo), "unknown") if selected_repo else "-",
            "branches": _branches(selected_repo) if selected_repo else [],
            "configPath": str(config_target / ".auto-ai-cr.json"),
        },
        "monitor": monitor.to_mapping(),
        "recentReviews": _merge_recent_reviews(
            _recent_reviews_all(target_list),
            recent_reviews_global(),
        ),
        "toolAvailability": _tool_availability(),
    }


def _discover_all_projects(targets: list[Path]) -> list[Path]:
    projects: list[Path] = []
    seen = set()
    for target in targets:
        for project in _discover_projects(target):
            key = str(project)
            if key in seen:
                continue
            seen.add(key)
            projects.append(project)
    return projects


def _discover_projects(target: Path) -> list[Path]:
    repo = try_find_repo(target)
    if repo is not None and repo == target:
        return [repo]
    projects: list[Path] = []
    if not target.is_dir():
        return projects
    skip = {".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__"}
    for child in sorted(target.iterdir()):
        if not child.is_dir() or child.name in skip or child.name.startswith("."):
            continue
        repo = try_find_repo(child)
        if repo is not None and repo == child.resolve():
            projects.append(repo)
    return projects


def _select_project(target: Path, projects: list[Path], selected_project: str) -> Path | None:
    if not projects:
        repo = try_find_repo(target)
        return repo if repo == target else None
    if selected_project:
        selected = Path(selected_project).expanduser().resolve()
        for project in projects:
            if project == selected:
                return project
    return projects[0]


def _review_repo(targets: list[Path], selected_project: str) -> Path:
    projects = _discover_all_projects(targets)
    repo = _select_project(targets[0], projects, selected_project)
    if repo is None:
        raise ValueError("请选择一个 Git 项目后再运行 CR")
    return repo


def _target_for_project(targets: list[Path], project: Path | None) -> Path | None:
    if project is None:
        return None
    resolved = project.resolve()
    best: Path | None = None
    for target in targets:
        target_path = target.resolve()
        if resolved == target_path:
            return target
        try:
            resolved.relative_to(target_path)
        except ValueError:
            continue
        if best is None or len(str(target_path)) > len(str(best)):
            best = target
    return best


def _write_config_all(targets: list[Path], config: AppConfig) -> None:
    for target in targets:
        write_config(target, config)


def _recent_reviews_all(targets: list[Path]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for target in targets:
        rows.extend(recent_reviews(target))
    rows.sort(
        key=lambda row: str(row.get("finishedAt") or row.get("startedAt") or row.get("queuedAt") or ""),
        reverse=True,
    )
    return rows[:8]


def _merge_recent_reviews(*groups: list[dict[str, object]]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    seen = set()
    for group in groups:
        for row in group:
            key = f"{row.get('repo')}|{row.get('sha')}|{row.get('reportPath')}"
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
    rows.sort(
        key=lambda row: str(row.get("finishedAt") or row.get("startedAt") or row.get("queuedAt") or ""),
        reverse=True,
    )
    return rows[:8]


def _load_ui_profile_targets() -> list[str]:
    profile = _load_ui_profile()
    targets = profile.get("targets", [])
    return [str(target) for target in targets if str(target).strip()] if isinstance(targets, list) else []


def _load_ui_profile() -> dict[str, object]:
    if not UI_STATE_PATH.exists():
        return {}
    try:
        with UI_STATE_PATH.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_ui_profile(targets: list[Path], selected_project: str = "") -> None:
    UI_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "targets": [str(target) for target in targets],
        "selectedProject": selected_project,
    }
    UI_STATE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _branches(repo: Path) -> list[str]:
    try:
        output = run_git(repo, ["branch", "--format=%(refname:short)"])
    except GitError:
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]


def _safe(callback, fallback: str) -> str:
    try:
        return callback()
    except Exception:
        return fallback


def _tool_availability() -> dict[str, dict[str, object]]:
    return {
        "codex": _command_status("codex"),
        "claude": _command_status("claude"),
        "cursor": _command_status("cursor-agent"),
    }


def _command_status(command: str) -> dict[str, object]:
    path = shutil.which(command)
    return {"installed": path is not None, "path": path or ""}


def _start_review_job(targets: list[Path], repo: Path, config: AppConfig) -> dict[str, object]:
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "status": "queued",
        "stage": "等待执行",
        "repo": str(repo),
        "createdAt": time.time(),
    }
    with _JOBS_LOCK:
        _JOBS[job_id] = job
    thread = threading.Thread(
        target=_run_review_job,
        args=(job_id, targets, repo, config),
        daemon=True,
    )
    thread.start()
    return dict(job)


def _run_review_job(job_id: str, targets: list[Path], repo: Path, config: AppConfig) -> None:
    try:
        _update_job(job_id, status="running", stage="收集 Git diff")
        result = _run_once(repo, config, job_id=job_id)
        status = _job_status_from_result(result)
        _update_job(
            job_id,
            status=status,
            stage=_job_finished_stage(status),
            finishedAt=time.time(),
            state=_state(targets, str(repo)),
            **result,
        )
    except Exception as exc:
        _update_job(
            job_id,
            status="failed",
            stage="CR 失败",
            error=str(exc),
            finishedAt=time.time(),
            state=_state(targets, str(repo)),
        )


def _update_job(job_id: str, **updates: object) -> None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        job.update(updates)


def _job_snapshot(job_id: str) -> dict[str, object]:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            raise ValueError(f"unknown job: {job_id}")
        return dict(job)


def _job_status_from_result(result: dict[str, object]) -> str:
    if result.get("skipped"):
        return "skipped"
    if int(result.get("exitCode") or 0) != 0:
        return "failed"
    return "done"


def _job_finished_stage(status: str) -> str:
    if status == "skipped":
        return "没有可审查的 diff"
    if status == "failed":
        return "CR 失败"
    return "CR 完成"


def _run_once(repo: Path, config: AppConfig, job_id: str | None = None) -> dict[str, object]:
    if job_id:
        _update_job(job_id, stage="收集 Git diff")
    diff = collect_diff(
        repo,
        DiffRequest(
            scope=config.scope,
            base_branch=config.base_branch,
            include=config.include,
            exclude=config.exclude,
            max_diff_chars=config.max_diff_chars,
        ),
    )
    record_review_started(repo, diff.head_sha, diff.scope, source="ui")
    if not diff.diff.strip():
        record_review_finished(repo, diff.head_sha, "skipped", issue_count=0, exit_code=0)
        if job_id:
            _update_job(job_id, stage="没有可审查的 diff")
        return {"message": "No diff to review.", "reportPath": None, "issues": [], "skipped": True}
    if job_id:
        _update_job(
            job_id,
            stage=f"调用 {config.tool} 执行 CR",
            head=diff.head_sha,
            scope=diff.scope,
            subject=diff.subject,
            diffChars=len(diff.diff),
            diffTruncated=diff.truncated,
        )
    try:
        result = run_review(repo, config, diff)
        record_review_finished(
            repo,
            diff.head_sha,
            "done" if result.exit_code == 0 else "failed",
            report_path=result.report_path,
            issues_path=result.issues_path,
            issue_count=len(result.issues),
            exit_code=result.exit_code,
        )
        if job_id:
            _update_job(job_id, stage="解析 CR 问题")
    except Exception as exc:
        record_review_finished(repo, diff.head_sha, "failed", error=str(exc))
        raise
    return {
        "message": f"Review finished: {result.report_path}",
        "reportPath": str(result.report_path),
        "issuesPath": str(result.issues_path),
        "issues": [issue.to_mapping() for issue in result.issues],
        "exitCode": result.exit_code,
    }


def _run_fix(
    repo: Path,
    config: AppConfig,
    issues: list,
    report_path: str,
) -> dict[str, object]:
    result = run_fix(
        repo,
        config,
        issues,
        Path(report_path).expanduser().resolve() if report_path else None,
    )
    return {
        "message": f"Fix finished: {result.output_path}",
        "fixReportPath": str(result.output_path),
        "exitCode": result.exit_code,
        "diff": result.diff,
        "gitStatus": result.status,
    }


def _generate_fix_prompt(
    repo: Path,
    config: AppConfig,
    issues: list,
    report_path: str,
) -> dict[str, object]:
    result = save_fix_prompt(
        repo,
        config,
        issues,
        Path(report_path).expanduser().resolve() if report_path else None,
    )
    return {
        "message": f"修复 Prompt 已生成: {result.prompt_path}",
        "promptPath": str(result.prompt_path),
        "prompt": result.prompt,
    }


def _read_report(report_path: str) -> dict[str, object]:
    path = _validated_report_path(report_path)
    content = path.read_text(encoding="utf-8", errors="replace")
    max_chars = 300_000
    truncated = len(content) > max_chars
    if truncated:
        content = content[:max_chars] + "\n\n[report truncated]\n"
    return {
        "path": str(path),
        "name": path.name,
        "content": content,
        "truncated": truncated,
    }


def _validated_report_path(report_path: str) -> Path:
    if not report_path:
        raise ValueError("缺少报告路径")
    path = Path(report_path).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise ValueError(f"报告不存在: {path}")
    if path.suffix.lower() not in {".md", ".json", ".txt"}:
        raise ValueError("只支持预览 Markdown、JSON 或文本报告")
    parts = set(path.parts)
    if ".auto-ai-cr" not in parts or "reviews" not in parts:
        raise ValueError("只能打开 auto-ai-cr 生成的报告")
    return path


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>auto-ai-cr</title>
  <style>
    :root {
      --bg: #f7f8fb;
      --panel: #ffffff;
      --ink: #1f2933;
      --muted: #667085;
      --line: #d9dee8;
      --accent: #1f7a68;
      --accent-dark: #14584b;
      --danger: #b42318;
      --code: #101828;
      --soft: #eef7f4;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }

    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
    }

    header {
      border-bottom: 1px solid var(--line);
      background: #fff;
    }

    .topbar {
      max-width: 1180px;
      margin: 0 auto;
      min-height: 68px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 0 24px;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
      min-width: 0;
    }

    .mark {
      width: 34px;
      height: 34px;
      display: grid;
      place-items: center;
      border-radius: 8px;
      color: #fff;
      background: var(--accent);
      font-weight: 800;
    }

    h1 {
      margin: 0;
      font-size: 20px;
      line-height: 1.2;
    }

    .repo-chip {
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      max-width: min(58vw, 720px);
    }

    main {
      max-width: 1180px;
      margin: 0 auto;
      padding: 22px 24px 36px;
      display: grid;
      grid-template-columns: minmax(0, 1fr) 340px;
      gap: 18px;
    }

    section, aside {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }

    section { padding: 20px; }
    aside { padding: 18px; height: fit-content; }

    .section-title {
      margin: 0 0 16px;
      font-size: 16px;
      line-height: 1.3;
    }

    .grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 16px;
    }

    .field {
      display: grid;
      gap: 7px;
      min-width: 0;
    }

    .field.wide { grid-column: 1 / -1; }

    label {
      color: #344054;
      font-size: 13px;
      font-weight: 650;
    }

    input, select, textarea {
      width: 100%;
      min-height: 40px;
      border: 1px solid #cbd3df;
      border-radius: 6px;
      padding: 9px 11px;
      color: var(--ink);
      background: #fff;
      font: inherit;
      font-size: 14px;
    }

    textarea {
      resize: vertical;
      min-height: 80px;
      line-height: 1.45;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }

    input:focus, select:focus, textarea:focus {
      outline: 2px solid rgba(31, 122, 104, 0.18);
      border-color: var(--accent);
    }

    .hint {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
    }

    .check-row {
      min-height: 40px;
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--ink);
      font-size: 14px;
      font-weight: 500;
    }

    .check-row input {
      width: 16px;
      min-height: 16px;
      margin: 0;
    }

    .tool-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }

    .tool-card {
      border: 1px solid #cbd3df;
      border-radius: 8px;
      background: #fff;
      padding: 12px;
      text-align: left;
      cursor: pointer;
    }

    .tool-card[aria-pressed="true"] {
      border-color: var(--accent);
      background: var(--soft);
      box-shadow: inset 0 0 0 1px var(--accent);
    }

    .tool-card strong {
      display: block;
      font-size: 14px;
      margin-bottom: 4px;
    }

    .tool-card span {
      display: block;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
    }

    .badge {
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      width: fit-content;
      margin-top: 8px;
      border-radius: 999px;
      padding: 0 8px;
      background: #ecfdf3;
      color: #067647;
      font-size: 12px;
      font-weight: 700;
    }

    .badge.missing {
      background: #fff1f0;
      color: var(--danger);
    }

    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 18px;
    }

    button {
      min-height: 40px;
      border: 1px solid #bcc6d3;
      border-radius: 6px;
      padding: 0 14px;
      background: #fff;
      color: var(--ink);
      font: inherit;
      font-weight: 700;
      cursor: pointer;
    }

    button.primary {
      color: #fff;
      background: var(--accent);
      border-color: var(--accent);
    }

    button.primary:hover { background: var(--accent-dark); }
    button:hover { border-color: #98a2b3; }
    button:disabled { cursor: wait; opacity: 0.65; }

    .status {
      min-height: 44px;
      margin-top: 16px;
      padding: 12px;
      border-radius: 6px;
      background: var(--soft);
      color: var(--accent-dark);
      font-size: 13px;
      line-height: 1.45;
      word-break: break-word;
    }

    .status.error {
      color: var(--danger);
      background: #fff1f0;
    }

    .process {
      display: grid;
      gap: 10px;
      margin-top: 16px;
      padding: 12px;
      border: 1px solid #cbd3df;
      border-radius: 8px;
      background: #fff;
    }

    .process-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }

    .process-title {
      font-size: 13px;
      font-weight: 800;
    }

    .process-pill {
      border-radius: 999px;
      padding: 3px 9px;
      background: #eef2f6;
      color: #344054;
      font-size: 12px;
      font-weight: 800;
    }

    .process-pill.running { background: #e6f4ff; color: #175cd3; }
    .process-pill.done { background: #ecfdf3; color: #067647; }
    .process-pill.skipped { background: #eef2f6; color: #475467; }
    .process-pill.failed { background: #fff1f0; color: var(--danger); }

    .step-list {
      display: grid;
      gap: 8px;
      margin: 0;
      padding: 0;
      list-style: none;
    }

    .step-list li {
      display: grid;
      grid-template-columns: 18px minmax(0, 1fr);
      gap: 8px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.35;
    }

    .dot {
      width: 10px;
      height: 10px;
      margin-top: 4px;
      border-radius: 999px;
      background: #cbd5e1;
    }

    .step-list li.active { color: var(--ink); font-weight: 700; }
    .step-list li.active .dot { background: #175cd3; box-shadow: 0 0 0 4px #e6f4ff; }
    .step-list li.done .dot { background: var(--accent); }
    .step-list li.failed .dot { background: var(--danger); }

    .review-panel {
      margin-top: 16px;
      border-top: 1px solid var(--line);
      padding-top: 16px;
    }

    .review-toolbar {
      display: flex;
      flex-wrap: wrap;
      align-items: end;
      gap: 10px;
      margin-bottom: 12px;
    }

    .review-toolbar .field {
      min-width: min(260px, 100%);
    }

    .issue-list {
      display: grid;
      gap: 10px;
      margin: 12px 0;
    }

    .issue-card {
      display: grid;
      grid-template-columns: 22px minmax(0, 1fr);
      gap: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: #fff;
    }

    .issue-card input {
      min-height: auto;
      margin-top: 3px;
    }

    .issue-title {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      margin-bottom: 6px;
      font-weight: 750;
    }

    .severity {
      border-radius: 999px;
      padding: 2px 7px;
      background: #eef2f6;
      color: #344054;
      font-size: 11px;
      font-weight: 800;
      text-transform: uppercase;
    }

    .severity.critical { background: #fff1f0; color: #b42318; }
    .severity.warning { background: #fff8e6; color: #93370d; }
    .severity.suggestion { background: #eef7f4; color: #14584b; }

    .issue-meta, .issue-body {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
      word-break: break-word;
    }

    .diff-box {
      max-height: 360px;
      overflow: auto;
      border: 1px solid #cbd3df;
      border-radius: 6px;
      padding: 12px;
      background: #101828;
      color: #f2f4f7;
      font-size: 12px;
      line-height: 1.45;
      white-space: pre-wrap;
    }

    .prompt-box {
      min-height: 260px;
      max-height: 520px;
      overflow: auto;
      white-space: pre-wrap;
    }

    .run-list {
      display: grid;
      gap: 10px;
    }

    .run-item {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      background: #fff;
    }

    .run-item strong {
      display: block;
      margin-bottom: 4px;
      font-size: 13px;
      word-break: break-word;
    }

    .run-item span, .run-item code {
      display: block;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
      word-break: break-word;
    }

    .report-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 8px;
    }

    .link-button {
      min-height: 30px;
      padding: 0 9px;
      border-color: #cbd3df;
      color: var(--accent-dark);
      font-size: 12px;
      font-weight: 800;
    }

    .report-viewer {
      margin-top: 16px;
      border-top: 1px solid var(--line);
      padding-top: 16px;
    }

    .report-box {
      width: 100%;
      min-height: 360px;
      max-height: 620px;
      overflow: auto;
      white-space: pre-wrap;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }

    .facts {
      display: grid;
      gap: 12px;
    }

    .fact {
      border-bottom: 1px solid var(--line);
      padding-bottom: 12px;
    }

    .fact:last-child {
      border-bottom: 0;
      padding-bottom: 0;
    }

    .fact span {
      display: block;
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 4px;
    }

    .fact strong, .fact code {
      display: block;
      color: var(--code);
      font-size: 13px;
      line-height: 1.4;
      word-break: break-word;
    }

    .split {
      display: grid;
      gap: 18px;
    }

    @media (max-width: 860px) {
      main {
        grid-template-columns: 1fr;
        padding: 16px;
      }
      .topbar {
        align-items: flex-start;
        flex-direction: column;
        padding: 14px 16px;
      }
      .repo-chip { max-width: 100%; white-space: normal; }
      .grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <div class="topbar">
      <div class="brand">
        <div class="mark">CR</div>
        <div>
          <h1>auto-ai-cr</h1>
          <div class="repo-chip" id="repoLabel">Loading...</div>
        </div>
      </div>
      <button id="refreshButton" type="button">刷新</button>
    </div>
  </header>

  <main>
    <div class="split">
      <section>
        <h2 class="section-title">Review 配置</h2>
        <div class="grid">
          <div class="field wide">
            <label for="repo">仓库或项目目录</label>
            <textarea id="repo" autocomplete="off" spellcheck="false" placeholder="/Users/me/code/project-a&#10;/Users/me/code/team-folder"></textarea>
            <div class="hint">支持多个目录，一行一个；可以是 Git 仓库，也可以是包含多个 Git 项目的上层目录。</div>
          </div>

          <div class="field wide" id="projectField">
            <label for="project">项目</label>
            <select id="project"></select>
            <div class="hint">多个目录下发现的 Git 项目都会出现在这里；daemon 会监听上方填写的所有目录。</div>
          </div>

          <div class="field">
            <label for="scope">CR 范围</label>
            <select id="scope">
              <option value="latest_commit">最新 commit</option>
              <option value="branch_diff">当前分支 vs 指定分支</option>
              <option value="worktree">工作区未暂存改动</option>
              <option value="staged">暂存区改动</option>
            </select>
          </div>

          <div class="field" id="baseField">
            <label for="base">对比分支</label>
            <input id="base" list="branches" placeholder="master" />
            <datalist id="branches"></datalist>
          </div>

          <div class="field">
            <label for="tool">CR 工具</label>
            <select id="tool">
              <option value="print">生成 Prompt 报告</option>
              <option value="codex">Codex CLI 自动 CR</option>
              <option value="claude">Claude Code 自动 CR</option>
              <option value="cursor">Cursor Agent 自动 CR</option>
              <option value="command">外部命令</option>
            </select>
          </div>

          <div class="field">
            <label for="maxDiff">最大 diff 字符数</label>
            <input id="maxDiff" type="number" min="1000" step="1000" />
          </div>

          <div class="field wide">
            <label>工具预设</label>
            <div class="tool-grid">
              <button class="tool-card" data-tool="codex" type="button">
                <strong>Codex CLI</strong>
                <span>使用 codex review 非交互执行 CR</span>
                <em class="badge missing" id="codexBadge">检测中</em>
              </button>
              <button class="tool-card" data-tool="claude" type="button">
                <strong>Claude Code</strong>
                <span>使用 claude -p 输出 Review 报告</span>
                <em class="badge missing" id="claudeBadge">检测中</em>
              </button>
              <button class="tool-card" data-tool="cursor" type="button">
                <strong>Cursor Agent</strong>
                <span>使用 cursor-agent 非交互执行 CR</span>
                <em class="badge missing" id="cursorBadge">检测中</em>
              </button>
              <button class="tool-card" data-tool="print" type="button">
                <strong>Prompt 报告</strong>
                <span>只生成给 AI 的 Prompt，不调用外部工具</span>
                <em class="badge">内置</em>
              </button>
              <button class="tool-card" data-tool="command" type="button">
                <strong>自定义命令</strong>
                <span>接入公司内部 CR 工具或其它 CLI</span>
                <em class="badge">可编辑</em>
              </button>
            </div>
          </div>

          <div class="field wide">
            <label for="command">外部命令</label>
            <textarea id="command" spellcheck="false"></textarea>
            <div class="hint">命令通过 stdin 接收完整 Review Prompt，支持 {repo}、{scope}、{base}、{head}、{report}。</div>
          </div>

          <div class="field">
            <label for="include">只审这些路径</label>
            <textarea id="include" spellcheck="false" placeholder="src/**&#10;tests/**"></textarea>
          </div>

          <div class="field">
            <label for="exclude">排除这些路径</label>
            <textarea id="exclude" spellcheck="false" placeholder="*.lock&#10;dist/**"></textarea>
          </div>

          <div class="field">
            <label for="reportsDir">报告目录</label>
            <input id="reportsDir" />
          </div>

          <div class="field">
            <label for="openReportAfterReview">报告打开</label>
            <label class="check-row">
              <input id="openReportAfterReview" type="checkbox" />
              <span>CR 完成后自动打开报告</span>
            </label>
          </div>

          <div class="field">
            <label for="reportOpenPreset">打开软件</label>
            <select id="reportOpenPreset">
              <option value="default">系统默认</option>
              <option value="textedit">文本预览 / TextEdit</option>
              <option value="vscode">VS Code</option>
              <option value="custom">自定义命令</option>
            </select>
          </div>

          <div class="field wide" id="reportOpenCommandField">
            <label for="reportOpenCommand">打开命令</label>
            <input id="reportOpenCommand" spellcheck="false" />
            <div class="hint">支持 {report}，例如 open -a TextEdit {report} 或 code {report}；留空时使用系统默认打开方式。</div>
          </div>

          <div class="field">
            <label for="pollInterval">监听间隔秒数</label>
            <input id="pollInterval" type="number" min="0.5" step="0.5" />
          </div>
        </div>

        <div class="actions">
          <button class="primary" id="saveButton" type="button">保存配置</button>
          <button id="runButton" type="button">运行一次 CR</button>
          <button id="hookButton" type="button">启用 auto-ai-cr daemon</button>
          <button id="stopMonitorButton" type="button">停用 auto-ai-cr daemon</button>
        </div>
        <div class="status" id="status">准备就绪</div>
        <div class="process" id="processPanel">
          <div class="process-head">
            <div class="process-title" id="processTitle">当前 CR 流程</div>
            <div class="process-pill" id="processBadge">空闲</div>
          </div>
          <ol class="step-list" id="stepList"></ol>
          <div class="hint" id="processMeta">手动运行或 daemon 触发后，会在这里显示进度。</div>
        </div>
        <div class="review-panel" id="reviewPanel" hidden>
          <h2 class="section-title">CR 问题与修复 Prompt</h2>
          <div class="review-toolbar">
            <div class="field">
              <label for="fixTool">目标 Agent</label>
              <select id="fixTool">
                <option value="codex">Codex</option>
                <option value="claude">Claude Code</option>
                <option value="cursor">Cursor Agent</option>
                <option value="command">自定义命令</option>
              </select>
            </div>
            <button class="primary" id="fixButton" type="button">生成修复 Prompt</button>
            <button id="copyPromptButton" type="button" disabled>复制 Prompt</button>
          </div>
          <div class="hint" id="reviewMeta"></div>
          <div class="issue-list" id="issueList"></div>
          <textarea class="prompt-box" id="fixPrompt" readonly hidden></textarea>
        </div>
        <div class="report-viewer" id="reportViewer" hidden>
          <h2 class="section-title">报告预览</h2>
          <div class="review-toolbar">
            <button id="openReportButton" type="button">用软件打开</button>
          </div>
          <div class="hint" id="reportMeta"></div>
          <textarea class="report-box" id="reportContent" readonly></textarea>
        </div>
      </section>
    </div>

    <aside>
      <h2 class="section-title">当前仓库</h2>
      <div class="facts">
        <div class="fact"><span>分支</span><strong id="branch">-</strong></div>
        <div class="fact"><span>HEAD</span><code id="head">-</code></div>
        <div class="fact"><span>配置文件</span><code id="configPath">-</code></div>
        <div class="fact"><span>auto-ai-cr daemon</span><strong id="hookState">-</strong></div>
        <div class="fact"><span>Trace2</span><code id="trace2State">-</code></div>
        <div class="fact"><span>事件日志</span><code id="eventPath">-</code></div>
        <div class="fact"><span>后台启动器</span><code id="monitorPath">-</code></div>
      </div>
      <h2 class="section-title" style="margin-top: 20px;">最近 CR</h2>
      <div class="run-list" id="recentRuns"></div>
    </aside>
  </main>

  <script>
    const els = {
      repo: document.querySelector("#repo"),
      project: document.querySelector("#project"),
      projectField: document.querySelector("#projectField"),
      repoLabel: document.querySelector("#repoLabel"),
      scope: document.querySelector("#scope"),
      base: document.querySelector("#base"),
      baseField: document.querySelector("#baseField"),
      branches: document.querySelector("#branches"),
      tool: document.querySelector("#tool"),
      maxDiff: document.querySelector("#maxDiff"),
      command: document.querySelector("#command"),
      include: document.querySelector("#include"),
      exclude: document.querySelector("#exclude"),
      reportsDir: document.querySelector("#reportsDir"),
      openReportAfterReview: document.querySelector("#openReportAfterReview"),
      reportOpenPreset: document.querySelector("#reportOpenPreset"),
      reportOpenCommand: document.querySelector("#reportOpenCommand"),
      reportOpenCommandField: document.querySelector("#reportOpenCommandField"),
      pollInterval: document.querySelector("#pollInterval"),
      status: document.querySelector("#status"),
      processPanel: document.querySelector("#processPanel"),
      processTitle: document.querySelector("#processTitle"),
      processBadge: document.querySelector("#processBadge"),
      stepList: document.querySelector("#stepList"),
      processMeta: document.querySelector("#processMeta"),
      reviewPanel: document.querySelector("#reviewPanel"),
      reviewMeta: document.querySelector("#reviewMeta"),
      issueList: document.querySelector("#issueList"),
      fixTool: document.querySelector("#fixTool"),
      fixButton: document.querySelector("#fixButton"),
      copyPromptButton: document.querySelector("#copyPromptButton"),
      fixPrompt: document.querySelector("#fixPrompt"),
      reportViewer: document.querySelector("#reportViewer"),
      reportMeta: document.querySelector("#reportMeta"),
      reportContent: document.querySelector("#reportContent"),
      openReportButton: document.querySelector("#openReportButton"),
      branch: document.querySelector("#branch"),
      head: document.querySelector("#head"),
      configPath: document.querySelector("#configPath"),
      hookState: document.querySelector("#hookState"),
      trace2State: document.querySelector("#trace2State"),
      eventPath: document.querySelector("#eventPath"),
      monitorPath: document.querySelector("#monitorPath"),
      recentRuns: document.querySelector("#recentRuns"),
      refreshButton: document.querySelector("#refreshButton"),
      saveButton: document.querySelector("#saveButton"),
      runButton: document.querySelector("#runButton"),
      hookButton: document.querySelector("#hookButton"),
      stopMonitorButton: document.querySelector("#stopMonitorButton")
    };

    const recommendedCommands = {
      codex: CODEX_COMMAND,
      claude: CLAUDE_COMMAND,
      cursor: CURSOR_COMMAND,
      command: "cat",
      print: ""
    };

    const recommendedFixCommands = {
      codex: CODEX_FIX_COMMAND,
      claude: CLAUDE_FIX_COMMAND,
      cursor: CURSOR_FIX_COMMAND,
      command: "cat"
    };

    const reportOpenPresets = {
      default: "",
      textedit: "open -a TextEdit {report}",
      vscode: VSCODE_OPEN_COMMAND,
      custom: ""
    };

    const commandBackups = {};
    let lastToolAvailability = {};
    let activeTool = "print";
    let lastReview = null;
    let activeJobId = null;
    let currentReportPath = "";

    function lines(value) {
      return value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    }

    function setBusy(busy) {
      for (const button of [els.refreshButton, els.saveButton, els.runButton, els.hookButton, els.stopMonitorButton, els.fixButton]) {
        button.disabled = busy;
      }
    }

    function setStatus(message, error = false) {
      els.status.textContent = message;
      els.status.classList.toggle("error", error);
    }

    function readConfig() {
      const selectedTool = els.tool.value;
      const codexCommand = selectedTool === "codex" ? els.command.value : commandBackups.codex;
      const claudeCommand = selectedTool === "claude" ? els.command.value : commandBackups.claude;
      const cursorCommand = selectedTool === "cursor" ? els.command.value : commandBackups.cursor;
      const customCommand = selectedTool === "command" ? els.command.value : commandBackups.command;
      const openPreset = els.reportOpenPreset.value;
      const openCommand = openPreset === "custom"
        ? els.reportOpenCommand.value
        : reportOpenPresets[openPreset] || "";
      return {
        scope: els.scope.value,
        base_branch: els.base.value || "master",
        tool: selectedTool,
        fix_tool: els.fixTool.value || selectedTool,
        tools: {
          print: { type: "print" },
          codex: { type: "command", command: commandWithDetectedPath("codex", codexCommand || recommendedCommands.codex, false) },
          claude: { type: "command", command: commandWithDetectedPath("claude", claudeCommand || recommendedCommands.claude, false) },
          cursor: { type: "command", command: commandWithDetectedPath("cursor", cursorCommand || recommendedCommands.cursor, false) },
          command: { type: "command", command: customCommand || recommendedCommands.command }
        },
        fix_tools: {
          codex: { type: "command", command: commandWithDetectedPath("codex", recommendedFixCommands.codex, true) },
          claude: { type: "command", command: commandWithDetectedPath("claude", recommendedFixCommands.claude, true) },
          cursor: { type: "command", command: commandWithDetectedPath("cursor", recommendedFixCommands.cursor, true) },
          command: { type: "command", command: recommendedFixCommands.command }
        },
        include: lines(els.include.value),
        exclude: lines(els.exclude.value),
        max_diff_chars: Number(els.maxDiff.value || 120000),
        reports_dir: els.reportsDir.value || DEFAULT_REPORTS_DIR,
        poll_interval_seconds: Number(els.pollInterval.value || 2),
        open_report_after_review: els.openReportAfterReview.checked,
        report_open_command: openCommand
      };
    }

    function commandWithDetectedPath(tool, command, fix) {
      const recommended = fix ? recommendedFixCommands[tool] : recommendedCommands[tool];
      const status = lastToolAvailability[tool];
      if (!status || !status.path || command !== recommended) return command;
      return replaceFirstToken(command, shellQuote(status.path));
    }

    function replaceFirstToken(command, token) {
      const index = command.indexOf(" ");
      return index === -1 ? token : token + command.slice(index);
    }

    function shellQuote(value) {
      return "'" + String(value).replaceAll("'", "'\\''") + "'";
    }

    function selectedCommand(config) {
      const tool = config.tool;
      if (tool === "print") return "";
      return commandBackups[tool] || (config.tools[tool] && config.tools[tool].command) || recommendedCommands[tool] || "cat";
    }

    function updateToolCards(tool, availability) {
      for (const card of document.querySelectorAll(".tool-card")) {
        card.setAttribute("aria-pressed", String(card.dataset.tool === tool));
      }
      setBadge("codexBadge", availability.codex);
      setBadge("claudeBadge", availability.claude);
      setBadge("cursorBadge", availability.cursor);
    }

    function setBadge(id, status) {
      const badge = document.querySelector("#" + id);
      const installed = status && status.installed;
      badge.textContent = installed ? "已检测到" : "未安装";
      badge.classList.toggle("missing", !installed);
      badge.title = installed ? status.path : "PATH 中未找到";
    }

    function switchTool(tool) {
      const previous = activeTool;
      if (previous !== "print") {
        commandBackups[previous] = els.command.value;
      }
      els.tool.value = tool;
      els.command.value = commandBackups[tool] || recommendedCommands[tool] || "";
      els.command.disabled = tool === "print";
      activeTool = tool;
      for (const card of document.querySelectorAll(".tool-card")) {
        card.setAttribute("aria-pressed", String(card.dataset.tool === tool));
      }
    }

    function render(state) {
      const config = state.config;
      lastToolAvailability = state.toolAvailability || {};
      els.repo.value = state.repo;
      els.repoLabel.textContent = (state.targets || [state.repo]).join(" · ");
      renderProjects(state.projects || [], state.selectedProject || "");
      els.scope.value = config.scope;
      els.base.value = config.base_branch;
      els.tool.value = config.tool;
      els.fixTool.value = config.fix_tool || (config.tool === "print" ? "codex" : config.tool);
      activeTool = config.tool;
      els.maxDiff.value = config.max_diff_chars;
      commandBackups.codex = commandWithDetectedPath("codex", (config.tools.codex && config.tools.codex.command) || recommendedCommands.codex, false);
      commandBackups.claude = commandWithDetectedPath("claude", (config.tools.claude && config.tools.claude.command) || recommendedCommands.claude, false);
      commandBackups.cursor = commandWithDetectedPath("cursor", (config.tools.cursor && config.tools.cursor.command) || recommendedCommands.cursor, false);
      commandBackups.command = (config.tools.command && config.tools.command.command) || recommendedCommands.command;
      els.command.value = selectedCommand(config);
      els.command.disabled = config.tool === "print";
      els.include.value = (config.include || []).join("\n");
      els.exclude.value = (config.exclude || []).join("\n");
      els.reportsDir.value = config.reports_dir;
      els.openReportAfterReview.checked = Boolean(config.open_report_after_review);
      renderOpenCommand(config.report_open_command || "");
      els.pollInterval.value = config.poll_interval_seconds;
      els.branch.textContent = state.git.branch;
      els.head.textContent = state.git.head;
      els.configPath.textContent = state.git.configPath;
      els.hookState.textContent = monitorText(state.monitor);
      els.trace2State.textContent = state.monitor.trace2Target || "未配置";
      els.eventPath.textContent = state.monitor.eventPath || state.monitor.socketPath;
      els.monitorPath.textContent = state.monitor.launcherPath || state.monitor.plistPath;
      els.branches.innerHTML = "";
      for (const branch of state.git.branches) {
        const option = document.createElement("option");
        option.value = branch;
        els.branches.appendChild(option);
      }
      updateToolCards(config.tool, state.toolAvailability || {});
      renderRecentRuns(state.recentReviews || []);
      syncScopeFields();
    }

    function renderOpenCommand(command) {
      const preset = presetForOpenCommand(command);
      els.reportOpenPreset.value = preset;
      els.reportOpenCommand.value = preset === "custom" ? command : reportOpenPresets[preset];
      syncOpenReportFields();
    }

    function presetForOpenCommand(command) {
      if (command === "code {report}") return "vscode";
      for (const [name, presetCommand] of Object.entries(reportOpenPresets)) {
        if (name !== "custom" && command === presetCommand) return name;
      }
      return "custom";
    }

    function syncOpenReportFields() {
      const preset = els.reportOpenPreset.value;
      const isCustom = preset === "custom";
      if (!isCustom) els.reportOpenCommand.value = reportOpenPresets[preset] || "";
      els.reportOpenCommand.disabled = !isCustom;
      els.reportOpenCommandField.style.opacity = isCustom ? "1" : "0.72";
    }

    function renderRecentRuns(runs) {
      els.recentRuns.innerHTML = "";
      if (!runs.length) {
        const empty = document.createElement("div");
        empty.className = "hint";
        empty.textContent = "暂无 CR 记录";
        els.recentRuns.appendChild(empty);
        renderProcessIdle();
        return;
      }
      if (!activeJobId) renderProcessFromRun(runs[0]);
      for (const run of runs) {
        const item = document.createElement("div");
        item.className = "run-item";
        const title = document.createElement("strong");
        title.textContent = statusText(run.status) + " · " + shortSha(run.sha) + " · " + (run.scope || "unknown");
        const repo = document.createElement("span");
        repo.textContent = run.repo || "";
        const meta = document.createElement("span");
        meta.textContent = [
          run.source || "manual",
          run.issueCount === undefined ? "" : "问题 " + run.issueCount,
          run.finishedAt || run.startedAt || run.queuedAt || ""
        ].filter(Boolean).join(" · ");
        item.append(title, repo, meta);
        if (run.reportPath) {
          const actions = document.createElement("div");
          actions.className = "report-actions";
          const preview = document.createElement("button");
          preview.type = "button";
          preview.className = "link-button";
          preview.textContent = "查看报告";
          preview.addEventListener("click", () => previewReport(run.reportPath));
          const open = document.createElement("button");
          open.type = "button";
          open.className = "link-button";
          open.textContent = "用软件打开";
          open.addEventListener("click", () => openReport(run.reportPath));
          actions.append(preview, open);
          const report = document.createElement("code");
          report.textContent = run.reportPath;
          item.append(actions, report);
        }
        if (run.error) {
          const error = document.createElement("span");
          error.textContent = "错误：" + run.error;
          item.appendChild(error);
        }
        els.recentRuns.appendChild(item);
      }
    }

    function renderProcessIdle() {
      renderProcess({
        status: "idle",
        stage: "等待触发",
        message: "手动运行或 daemon 触发后，会在这里显示进度。"
      });
    }

    function renderProcessFromRun(run) {
      const runStatus = run.status || (run.reportPath ? "done" : "queued");
      renderProcess({
        status: runStatus,
        stage: runStatus === "done" ? "CR 完成" : runStatus === "failed" ? "CR 失败" : runStatus === "skipped" ? "没有可审查的 diff" : "等待执行",
        message: [run.repo, run.reportPath ? "报告：" + run.reportPath : ""].filter(Boolean).join(" · ")
      });
    }

    function renderProcess(job) {
      const status = job.status || "idle";
      const stage = job.stage || "等待触发";
      els.processBadge.textContent = statusText(status);
      els.processBadge.className = "process-pill " + status;
      els.processTitle.textContent = stage;
      els.processMeta.textContent = job.message || job.error || job.reportPath || "";
      const steps = ["收集 Git diff", "调用 AI 工具", "解析 CR 问题", "生成报告"];
      const active = stepIndex(stage, status);
      els.stepList.innerHTML = "";
      steps.forEach((step, index) => {
        const item = document.createElement("li");
        if (status === "failed" && index === active) item.className = "failed";
        else if (status === "done" || index < active) item.className = "done";
        else if (index === active && status !== "idle") item.className = "active";
        const dot = document.createElement("span");
        dot.className = "dot";
        const text = document.createElement("span");
        text.textContent = step;
        item.append(dot, text);
        els.stepList.appendChild(item);
      });
    }

    function stepIndex(stage, status) {
      if (status === "done") return 4;
      if (String(stage).includes("收集")) return 0;
      if (String(stage).includes("调用")) return 1;
      if (String(stage).includes("解析")) return 2;
      if (String(stage).includes("完成")) return 3;
      return 0;
    }

    function statusText(status) {
      return {
        idle: "空闲",
        queued: "排队中",
        running: "运行中",
        done: "已完成",
        failed: "失败",
        skipped: "无 diff"
      }[status] || status || "-";
    }

    function shortSha(sha) {
      return sha ? String(sha).slice(0, 8) : "unknown";
    }

    function renderProjects(projects, selectedProject) {
      els.project.innerHTML = "";
      for (const project of projects) {
        const option = document.createElement("option");
        option.value = project.path;
        option.textContent = project.name + " — " + project.path;
        if (project.target) option.title = "来源目录：" + project.target;
        option.selected = project.path === selectedProject;
        els.project.appendChild(option);
      }
      els.projectField.style.display = projects.length > 1 ? "grid" : "none";
    }

    function syncScopeFields() {
      const needsBase = els.scope.value === "branch_diff";
      els.baseField.style.display = needsBase ? "grid" : "none";
    }

    function monitorText(monitor) {
      if (monitor.running && monitor.repoWatched) return "运行中";
      if (monitor.running) return "daemon 运行中，本仓库未启用";
      if (monitor.installed && monitor.repoWatched) return "已安装，未运行";
      return "未启用";
    }

    async function api(path, body) {
      const response = await fetch(path, {
        method: body ? "POST" : "GET",
        headers: body ? { "Content-Type": "application/json" } : {},
        body: body ? JSON.stringify(body) : undefined
      });
      const data = await response.json();
      if (!response.ok || data.ok === false) {
        throw new Error(data.error || "操作失败");
      }
      return data;
    }

    async function load() {
      setBusy(true);
      try {
        const repo = encodeURIComponent(els.repo.value || "");
        const project = encodeURIComponent(els.project.value || "");
        const data = await api("/api/state?repo=" + repo + "&project=" + project);
        render(data);
        setStatus("配置已加载");
      } catch (error) {
        setStatus(error.message, true);
      } finally {
        setBusy(false);
      }
    }

    async function post(path, success) {
      setBusy(true);
      try {
        const data = await api(path, { repo: els.repo.value, project: els.project.value, config: readConfig() });
        if (data.state) render(data.state);
        setStatus(data.message || success);
      } catch (error) {
        setStatus(error.message, true);
      } finally {
        setBusy(false);
      }
    }

    async function startReview() {
      setBusy(true);
      try {
        const data = await api("/api/review/start", {
          repo: els.repo.value,
          project: els.project.value,
          config: readConfig()
        });
        if (data.state) render(data.state);
        activeJobId = data.job.id;
        renderProcess(data.job);
        setStatus("CR 已触发，正在执行");
        pollJob(activeJobId);
      } catch (error) {
        setBusy(false);
        setStatus(error.message, true);
      }
    }

    async function pollJob(jobId) {
      try {
        const data = await api("/api/job?id=" + encodeURIComponent(jobId));
        const job = data.job;
        renderProcess(job);
        if (job.status === "queued" || job.status === "running") {
          setTimeout(() => pollJob(jobId), 1000);
          return;
        }
        activeJobId = null;
        setBusy(false);
        if (job.state) {
          render(job.state);
          renderRecentRuns(prependRun(job.state.recentReviews || [], jobToRun(job)));
        } else {
          renderRecentRuns([jobToRun(job)]);
        }
        renderProcess(job);
        if (job.status === "done" || job.status === "skipped") {
          renderReviewResult(job);
          setStatus(job.message || (job.status === "skipped" ? "没有可审查的 diff" : "CR 已完成"));
        } else {
          setStatus(job.error || "CR 失败", true);
        }
      } catch (error) {
        activeJobId = null;
        setBusy(false);
        setStatus(error.message, true);
      }
    }

    function jobToRun(job) {
      return {
        status: job.status,
        source: "ui",
        scope: job.scope,
        repo: job.repo,
        sha: job.head,
        finishedAt: new Date().toISOString(),
        reportPath: job.reportPath,
        issuesPath: job.issuesPath,
        issueCount: (job.issues || []).length,
        error: job.error
      };
    }

    function prependRun(runs, run) {
      const key = run.repo + "|" + run.sha + "|" + run.reportPath;
      const filtered = runs.filter((item) => (item.repo + "|" + item.sha + "|" + item.reportPath) !== key);
      return [run, ...filtered].slice(0, 8);
    }

    async function previewReport(reportPath) {
      if (!reportPath) return;
      setBusy(true);
      try {
        const data = await api("/api/report?path=" + encodeURIComponent(reportPath));
        currentReportPath = data.path;
        els.reportViewer.hidden = false;
        els.reportMeta.textContent = (data.truncated ? "已截断 · " : "") + data.path;
        els.reportContent.value = data.content || "";
        els.reportViewer.scrollIntoView({ behavior: "smooth", block: "start" });
        setStatus("报告已加载");
      } catch (error) {
        setStatus(error.message, true);
      } finally {
        setBusy(false);
      }
    }

    async function openReport(reportPath) {
      const path = reportPath || currentReportPath;
      if (!path) {
        setStatus("请先选择一份报告", true);
        return;
      }
      setBusy(true);
      try {
        const data = await api("/api/report/open", {
          repo: els.repo.value,
          project: els.project.value,
          config: readConfig(),
          reportPath: path
        });
        setStatus(data.message || "报告已打开");
      } catch (error) {
        setStatus(error.message, true);
      } finally {
        setBusy(false);
      }
    }

    function renderReviewResult(data) {
      lastReview = {
        reportPath: data.reportPath,
        issuesPath: data.issuesPath,
        issues: data.issues || []
      };
      els.reviewPanel.hidden = false;
      els.fixPrompt.hidden = true;
      els.fixPrompt.value = "";
      els.copyPromptButton.disabled = true;
      els.reviewMeta.textContent = data.reportPath
        ? "报告：" + data.reportPath + "；问题数：" + lastReview.issues.length
        : "没有生成报告";
      renderIssues(lastReview.issues);
    }

    function renderIssues(issues) {
      els.issueList.innerHTML = "";
      if (!issues.length) {
        const empty = document.createElement("div");
        empty.className = "hint";
        empty.textContent = "没有发现可选择的问题。";
        els.issueList.appendChild(empty);
        return;
      }
      for (const issue of issues) {
        const card = document.createElement("label");
        card.className = "issue-card";
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.value = issue.id;
        checkbox.checked = issue.severity !== "suggestion";
        const body = document.createElement("div");
        const title = document.createElement("div");
        title.className = "issue-title";
        const severity = document.createElement("span");
        severity.className = "severity " + (issue.severity || "warning");
        severity.textContent = issue.severity || "warning";
        const titleText = document.createElement("span");
        titleText.textContent = issue.id + " · " + issue.title;
        title.append(severity, titleText);
        const meta = document.createElement("div");
        meta.className = "issue-meta";
        meta.textContent = [issue.file, issue.line ? "L" + issue.line : ""].filter(Boolean).join(":");
        const detail = document.createElement("div");
        detail.className = "issue-body";
        detail.textContent = issue.recommendation || issue.description || issue.risk || "";
        body.append(title, meta, detail);
        card.append(checkbox, body);
        els.issueList.appendChild(card);
      }
    }

    function selectedIssues() {
      if (!lastReview) return [];
      const checked = new Set(Array.from(els.issueList.querySelectorAll("input[type=checkbox]:checked")).map((input) => input.value));
      return lastReview.issues.filter((issue) => checked.has(issue.id));
    }

    async function generateFixPrompt() {
      if (!lastReview) {
        setStatus("请先运行一次 CR", true);
        return;
      }
      const issues = selectedIssues();
      if (!issues.length) {
        setStatus("请选择至少一个问题", true);
        return;
      }
      setBusy(true);
      try {
        const data = await api("/api/fix-prompt", {
          repo: els.repo.value,
          project: els.project.value,
          config: readConfig(),
          reportPath: lastReview.reportPath,
          issues
        });
        if (data.state) render(data.state);
        els.fixPrompt.hidden = false;
        els.fixPrompt.value = data.prompt || "";
        els.copyPromptButton.disabled = !els.fixPrompt.value;
        setStatus(data.message || "修复 Prompt 已生成");
      } catch (error) {
        setStatus(error.message, true);
      } finally {
        setBusy(false);
      }
    }

    async function copyPrompt() {
      if (!els.fixPrompt.value) return;
      try {
        await navigator.clipboard.writeText(els.fixPrompt.value);
        setStatus("Prompt 已复制");
      } catch (error) {
        els.fixPrompt.focus();
        els.fixPrompt.select();
        setStatus("已选中 Prompt，可手动复制");
      }
    }

    els.refreshButton.addEventListener("click", load);
    els.project.addEventListener("change", load);
    els.scope.addEventListener("change", syncScopeFields);
    els.reportOpenPreset.addEventListener("change", syncOpenReportFields);
    els.tool.addEventListener("change", () => switchTool(els.tool.value));
    for (const card of document.querySelectorAll(".tool-card")) {
      card.addEventListener("click", () => switchTool(card.dataset.tool));
    }
    els.saveButton.addEventListener("click", () => post("/api/config", "配置已保存"));
    els.runButton.addEventListener("click", startReview);
    els.hookButton.addEventListener("click", () => post("/api/monitor", "auto-ai-cr daemon 已启用"));
    els.stopMonitorButton.addEventListener("click", () => post("/api/monitor/stop", "auto-ai-cr daemon 已停用"));
    els.fixButton.addEventListener("click", generateFixPrompt);
    els.copyPromptButton.addEventListener("click", copyPrompt);
    els.openReportButton.addEventListener("click", () => openReport(currentReportPath));

    load();
  </script>
</body>
</html>
""".replace("CODEX_COMMAND", json.dumps(CODEX_REVIEW_COMMAND)).replace(
    "CLAUDE_COMMAND", json.dumps(CLAUDE_REVIEW_COMMAND)
).replace("CURSOR_COMMAND", json.dumps(CURSOR_REVIEW_COMMAND)).replace(
    "CODEX_FIX_COMMAND", json.dumps(CODEX_FIX_COMMAND)
).replace("CLAUDE_FIX_COMMAND", json.dumps(CLAUDE_FIX_COMMAND)).replace(
    "CURSOR_FIX_COMMAND", json.dumps(CURSOR_FIX_COMMAND)
).replace(
    "VSCODE_OPEN_COMMAND", json.dumps(_vscode_open_command())
).replace(
    "DEFAULT_REPORTS_DIR", json.dumps(DEFAULT_REPORTS_DIR)
)

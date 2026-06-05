from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import webbrowser

from .config import AppConfig, load_config, write_config
from .git_ops import (
    DiffRequest,
    GitError,
    collect_diff,
    current_branch,
    find_repo,
    head_sha,
    run_git,
)
from .hooks import install_post_commit_hook
from .reviewer import run_review


DEFAULT_PORT = 8765


def serve_ui(
    repo: Path,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    open_browser: bool = False,
) -> None:
    _ensure_loopback_host(host)
    root_repo = find_repo(repo)
    handler = _handler(root_repo)
    server = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{server.server_port}"
    print(f"auto-ai-cr ui: {url}")
    if open_browser:
        webbrowser.open(url)
    server.serve_forever()


def _handler(default_repo: Path) -> type[BaseHTTPRequestHandler]:
    class UIHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._html(HTML)
                return
            if parsed.path == "/api/state":
                params = parse_qs(parsed.query)
                repo = _repo_from_value(default_repo, params.get("repo", [""])[0])
                self._json(_state(repo))
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            try:
                data = self._read_json()
                repo = _repo_from_payload(default_repo, data)
                if self.path == "/api/config":
                    config = AppConfig.from_mapping(data["config"])
                    write_config(repo, config)
                    self._json({"ok": True, "state": _state(repo)})
                    return
                if self.path == "/api/review":
                    config = AppConfig.from_mapping(data["config"])
                    write_config(repo, config)
                    result = _run_once(repo, config)
                    self._json({"ok": True, **result, "state": _state(repo)})
                    return
                if self.path == "/api/hook":
                    config = AppConfig.from_mapping(data["config"])
                    write_config(repo, config)
                    hook_path = install_post_commit_hook(repo)
                    self._json(
                        {
                            "ok": True,
                            "message": f"Hook installed: {hook_path}",
                            "state": _state(repo),
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


def _repo_from_payload(default_repo: Path, data: dict[str, object]) -> Path:
    return _repo_from_value(default_repo, str(data.get("repo") or ""))


def _repo_from_value(default_repo: Path, value: str) -> Path:
    repo_value = value or str(default_repo)
    return find_repo(Path(repo_value).expanduser().resolve())


def _state(repo: Path) -> dict[str, object]:
    config = load_config(repo)
    return {
        "repo": str(repo),
        "config": config.to_mapping(),
        "git": {
            "branch": _safe(lambda: current_branch(repo), "unknown"),
            "head": _safe(lambda: head_sha(repo), "unknown"),
            "branches": _branches(repo),
            "hookInstalled": (repo / ".git" / "hooks" / "post-commit").exists(),
            "configPath": str(repo / ".auto-ai-cr.json"),
        },
    }


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


def _run_once(repo: Path, config: AppConfig) -> dict[str, object]:
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
    if not diff.diff.strip():
        return {"message": "No diff to review.", "reportPath": None}
    result = run_review(repo, config, diff)
    return {
        "message": f"Review finished: {result.report_path}",
        "reportPath": str(result.report_path),
        "exitCode": result.exit_code,
    }


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
            <label for="repo">仓库路径</label>
            <input id="repo" autocomplete="off" />
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

          <div class="field">
            <label for="base">对比分支</label>
            <input id="base" list="branches" placeholder="master" />
            <datalist id="branches"></datalist>
          </div>

          <div class="field">
            <label for="tool">CR 工具</label>
            <select id="tool">
              <option value="print">生成 Prompt 报告</option>
              <option value="command">外部命令</option>
            </select>
          </div>

          <div class="field">
            <label for="maxDiff">最大 diff 字符数</label>
            <input id="maxDiff" type="number" min="1000" step="1000" />
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
            <label for="pollInterval">监听间隔秒数</label>
            <input id="pollInterval" type="number" min="0.5" step="0.5" />
          </div>
        </div>

        <div class="actions">
          <button class="primary" id="saveButton" type="button">保存配置</button>
          <button id="runButton" type="button">运行一次 CR</button>
          <button id="hookButton" type="button">安装 commit hook</button>
        </div>
        <div class="status" id="status">准备就绪</div>
      </section>
    </div>

    <aside>
      <h2 class="section-title">当前仓库</h2>
      <div class="facts">
        <div class="fact"><span>分支</span><strong id="branch">-</strong></div>
        <div class="fact"><span>HEAD</span><code id="head">-</code></div>
        <div class="fact"><span>配置文件</span><code id="configPath">-</code></div>
        <div class="fact"><span>commit hook</span><strong id="hookState">-</strong></div>
      </div>
    </aside>
  </main>

  <script>
    const els = {
      repo: document.querySelector("#repo"),
      repoLabel: document.querySelector("#repoLabel"),
      scope: document.querySelector("#scope"),
      base: document.querySelector("#base"),
      branches: document.querySelector("#branches"),
      tool: document.querySelector("#tool"),
      maxDiff: document.querySelector("#maxDiff"),
      command: document.querySelector("#command"),
      include: document.querySelector("#include"),
      exclude: document.querySelector("#exclude"),
      reportsDir: document.querySelector("#reportsDir"),
      pollInterval: document.querySelector("#pollInterval"),
      status: document.querySelector("#status"),
      branch: document.querySelector("#branch"),
      head: document.querySelector("#head"),
      configPath: document.querySelector("#configPath"),
      hookState: document.querySelector("#hookState"),
      refreshButton: document.querySelector("#refreshButton"),
      saveButton: document.querySelector("#saveButton"),
      runButton: document.querySelector("#runButton"),
      hookButton: document.querySelector("#hookButton")
    };

    function lines(value) {
      return value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    }

    function setBusy(busy) {
      for (const button of [els.refreshButton, els.saveButton, els.runButton, els.hookButton]) {
        button.disabled = busy;
      }
    }

    function setStatus(message, error = false) {
      els.status.textContent = message;
      els.status.classList.toggle("error", error);
    }

    function readConfig() {
      return {
        scope: els.scope.value,
        base_branch: els.base.value || "master",
        tool: els.tool.value,
        tools: {
          print: { type: "print" },
          command: { type: "command", command: els.command.value || "cat" }
        },
        include: lines(els.include.value),
        exclude: lines(els.exclude.value),
        max_diff_chars: Number(els.maxDiff.value || 120000),
        reports_dir: els.reportsDir.value || ".auto-ai-cr/reviews",
        poll_interval_seconds: Number(els.pollInterval.value || 2)
      };
    }

    function render(state) {
      const config = state.config;
      els.repo.value = state.repo;
      els.repoLabel.textContent = state.repo;
      els.scope.value = config.scope;
      els.base.value = config.base_branch;
      els.tool.value = config.tool;
      els.maxDiff.value = config.max_diff_chars;
      els.command.value = (config.tools.command && config.tools.command.command) || "cat";
      els.include.value = (config.include || []).join("\n");
      els.exclude.value = (config.exclude || []).join("\n");
      els.reportsDir.value = config.reports_dir;
      els.pollInterval.value = config.poll_interval_seconds;
      els.branch.textContent = state.git.branch;
      els.head.textContent = state.git.head;
      els.configPath.textContent = state.git.configPath;
      els.hookState.textContent = state.git.hookInstalled ? "已安装" : "未安装";
      els.branches.innerHTML = "";
      for (const branch of state.git.branches) {
        const option = document.createElement("option");
        option.value = branch;
        els.branches.appendChild(option);
      }
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
        const data = await api("/api/state?repo=" + repo);
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
        const data = await api(path, { repo: els.repo.value, config: readConfig() });
        if (data.state) render(data.state);
        setStatus(data.message || success);
      } catch (error) {
        setStatus(error.message, true);
      } finally {
        setBusy(false);
      }
    }

    els.refreshButton.addEventListener("click", load);
    els.saveButton.addEventListener("click", () => post("/api/config", "配置已保存"));
    els.runButton.addEventListener("click", () => post("/api/review", "CR 已完成"));
    els.hookButton.addEventListener("click", () => post("/api/hook", "Hook 已安装"));

    load();
  </script>
</body>
</html>
"""

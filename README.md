# auto-ai-cr

本地 Git 提交监听 + 自动 AI Code Review。

它解决三个问题：

- 监听本机仓库的 commit：内置 `auto-ai-cr daemon`，通过 Git Trace2 接收 commit 成功事件，也保留手动运行能力。
- 选择 CR 范围：最新 commit、当前分支相对 master/main/指定分支的 diff、工作区 diff、暂存区 diff。
- 选择 CR 工具：内置 `print` 输出，也支持任意命令模板，例如 Codex、Claude、OpenAI CLI 或内部 CR 服务。
- 看得见 CR 过程：UI 会显示手动 CR 的执行阶段，也会展示 daemon 最近触发的 CR 记录。
- 生成修复 Prompt：CR 后勾选问题，为 Codex / Claude Code / Cursor Agent 生成只处理选中问题的修复 prompt。

## 快速开始

macOS / Linux 一行安装：

```bash
curl -fsSL https://github.com/lzwcyd/auto-ai-cr/releases/latest/download/install.sh | bash
```

Windows PowerShell：

```powershell
irm https://github.com/lzwcyd/auto-ai-cr/releases/latest/download/install.ps1 | iex
```

Windows Git Bash 也可以使用 `install.sh`。Release 会自动提供这些二进制：

- `auto-ai-cr-macos-arm64.tar.gz`
- `auto-ai-cr-macos-x64.tar.gz`
- `auto-ai-cr-linux-x64.tar.gz`
- `auto-ai-cr-windows-x64.zip`

启动配置页面：

```bash
auto-ai-cr --version
auto-ai-cr help
auto-ai-cr ui --open
```

默认 UI 端口是 `8765`；如果端口已被占用，会自动尝试后续端口。也可以手动指定：

```bash
auto-ai-cr ui --open --port 8766
```

源码安装：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .

auto-ai-cr init
auto-ai-cr ui --open
```

初始化会生成 `.auto-ai-cr.json`：

```json
{
  "scope": "latest_commit",
  "base_branch": "master",
  "tool": "print",
  "tools": {
    "print": {
      "type": "print"
    },
    "codex": {
      "type": "command",
      "command": "codex review -"
    },
    "claude": {
      "type": "command",
      "command": "claude -p --permission-mode dontAsk --output-format text"
    },
    "cursor": {
      "type": "command",
      "command": "cursor-agent -p --output-format text"
    },
    "command": {
      "type": "command",
      "command": "cat"
    }
  },
  "fix_tool": "codex",
  "fix_tools": {
    "codex": {
      "type": "command",
      "command": "codex exec --sandbox workspace-write --ask-for-approval never -"
    },
    "claude": {
      "type": "command",
      "command": "claude -p --permission-mode acceptEdits --output-format text"
    },
    "cursor": {
      "type": "command",
      "command": "cursor-agent -p --output-format text --trust"
    },
    "command": {
      "type": "command",
      "command": "cat"
    }
  },
  "include": [],
  "exclude": [],
  "max_diff_chars": 120000,
  "reports_dir": "~/.auto-ai-cr/reviews",
  "poll_interval_seconds": 2.0,
  "open_report_after_review": false,
  "report_open_command": ""
}
```

## 运行模式

打开本地配置页面：

```bash
auto-ai-cr ui --open
```

配置页面可以保存 `.auto-ai-cr.json`、运行一次 CR、启用或停用 auto-ai-cr daemon。
UI 只绑定本机地址，避免外部机器触发本地命令型 CR 工具。

`仓库或项目目录` 可以填单个 Git 仓库，也可以填一个包含多个 Git 项目的上层目录；支持填写多个目录，一行一个。UI 会把路径列表和上次选择的项目保存到 `~/.auto-ai-cr/ui.json`，下次打开会自动恢复。保存配置时会把同一份 `.auto-ai-cr.json` 写入所有路径；daemon 会监听这些目录下的所有 Git 项目；手动运行 CR 时可在页面里选择具体项目。

页面会自动检测本机是否安装了 `codex`、`claude` 和 `cursor-agent`，并提供内置工具卡片：

- Codex CLI：默认命令 `codex review -`
- Claude Code：默认命令 `claude -p --permission-mode dontAsk --output-format text`
- Cursor Agent：默认命令 `cursor-agent -p --output-format text`
- Prompt 报告：只生成 Review Prompt 和 diff
- 自定义命令：接入内部 CR 工具或其它 CLI

运行一次 CR 后，页面会展示当前阶段：收集 Git diff、调用 AI 工具、解析 CR 问题、生成报告。右侧的“最近 CR”会显示 daemon 或手动触发的记录、commit、状态、问题数量和报告路径。报告默认写入 `~/.auto-ai-cr/reviews`，也可以在 UI 里改成其它目录。

报告默认采用摘要式结构：先看结论、必须处理的问题、可选建议和测试建议；工具命令、退出码、stderr 等运行信息会折叠到报告底部，避免一打开就是日志。

CR 完成后可以自动打开报告，支持系统默认打开方式、TextEdit、VS Code 或自定义命令。自定义命令支持 `{report}` 占位符，例如：

```bash
open -a TextEdit {report}
open -a 'Visual Studio Code' {report}
```

CR 完成后，页面会展示结构化问题列表。你可以勾选要修复的问题，再选择 Codex、Claude Code、Cursor Agent 或自定义目标生成修复 Prompt。Prompt 只包含你勾选的问题，并会保存到 `~/.auto-ai-cr/reviews/fix-prompts/`，方便复制到对应 agent 中执行。

监听当前仓库 HEAD 变化：

```bash
auto-ai-cr watch
```

启用 auto-ai-cr daemon：

```bash
auto-ai-cr install-monitor
```

也可以监听一个项目目录下的所有仓库：

```bash
auto-ai-cr install-monitor --repo ~/code/github
```

启用后链路是：

```text
git commit 成功
-> Git Trace2 追加 JSON 事件到本机事件日志
-> auto-ai-cr daemon 识别 cmd_name=commit 且 exit code=0
-> auto-ai-cr daemon 读取当前 HEAD 得到 commit SHA
-> 按当前配置调用 Codex CLI / Claude Code / 自定义命令
-> 写报告到 .auto-ai-cr/reviews
-> 写入本地 refs/notes/codex-cr
```

启用 daemon 会把全局 `trace2.eventtarget` 指向 `~/.auto-ai-cr/daemon/trace2-event.jsonl`。这个文件型 Trace2 target 可在 macOS、Linux、Windows 上工作；若原来已有 target，`auto-ai-cr` 会记录它，卸载最后一个监听仓库时会尝试恢复。

查看状态：

```bash
auto-ai-cr monitor-status
```

停用 auto-ai-cr daemon：

```bash
auto-ai-cr uninstall-monitor
```

底层仍保留 Git post-commit hook 方式，主要用于不想开启 daemon 的环境：

```bash
auto-ai-cr install-hook
```

安装后，每次 `git commit` 成功都会执行：

```bash
auto-ai-cr run --scope latest_commit
```

hook 使用非阻塞策略，CR 命令失败不会让本次 commit 变成失败。

## CR 范围

```bash
# 最新一次 commit
auto-ai-cr run --scope latest_commit

# 当前分支相对 master 的差异
auto-ai-cr run --scope branch_diff --base master

# 当前分支相对指定分支的差异
auto-ai-cr run --scope branch_diff --base release/2026-06

# 工作区未暂存差异
auto-ai-cr run --scope worktree

# 暂存区差异
auto-ai-cr run --scope staged
```

## 接入 CR 工具

默认 `print` 工具会把 prompt 和 diff 写入报告文件，便于先验证范围是否正确。

选择 Codex CLI：

```json
{
  "tool": "codex"
}
```

选择 Claude Code：

```json
{
  "tool": "claude"
}
```

选择 Cursor Agent CLI：

```json
{
  "tool": "cursor"
}
```

你可以把 `.auto-ai-cr.json` 改成：

```json
{
  "tool": "command",
  "tools": {
    "command": {
      "type": "command",
      "command": "your-review-cli --model best-reviewer"
    }
  }
}
```

`command` 会通过 stdin 收到完整 review prompt，并把 stdout/stderr 保存到报告文件。

命令也支持模板变量：

- `{repo}`：仓库根目录
- `{scope}`：CR 范围
- `{base}`：base 分支
- `{head}`：当前 HEAD sha
- `{report}`：报告文件路径

例如：

```json
{
  "command": "internal-cr --repo {repo} --scope {scope} --out {report}"
}
```

## 选择问题并生成修复 Prompt

CR 工具会被要求在报告末尾输出一个 `auto-ai-cr-issues` JSON 代码块。`auto-ai-cr` 会把它保存为同名 `.issues.json` 文件，并在 UI 中渲染成可勾选的问题卡片。

生成的修复 Prompt 会包含：

- 用户勾选的问题列表
- 问题的文件、行号、风险和建议
- 只修复选中问题、不要自动提交 commit 的约束
- 面向 Codex / Claude Code / Cursor Agent 的执行提示

Prompt 会展示在 UI 中，并保存到：

```text
~/.auto-ai-cr/reviews/fix-prompts/
```

底层仍保留命令式修复能力用于后续自动化集成，但 UI 的主路径是生成 Prompt，由你决定粘贴给哪个 agent、何时执行、执行后是否提交。

## 文件过滤

`include` 和 `exclude` 使用 git pathspec，传给 `git diff` / `git show`：

```json
{
  "include": ["src/**", "tests/**"],
  "exclude": ["*.lock", "dist/**"]
}
```

## 设计边界

这是一个本地 MVP，重点是把监听、diff 范围、工具适配跑通。后续适合继续扩展：

- 菜单栏应用或系统托盘入口。
- PR/MR 评论发布。
- 增量缓存，避免同一个 commit 重复 CR。
- 更丰富的 CR 工具适配器，例如 OpenAI Responses API、企业内部网关、钉钉/飞书通知。

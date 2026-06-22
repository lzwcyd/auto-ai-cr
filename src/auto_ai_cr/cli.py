from __future__ import annotations

import argparse
from pathlib import Path
import sys

from . import __version__
from .config import AppConfig, load_config, write_default_config
from .git_ops import DiffRequest, GitError, collect_diff, find_repo
from .hooks import install_post_commit_hook
from .monitor import (
    install_monitor,
    monitor_status,
    record_review_finished,
    record_review_started,
    run_monitor,
    uninstall_monitor,
)
from .reviewer import run_review
from .web_ui import DEFAULT_PORT, serve_ui
from .watcher import watch_head


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "help":
        return _print_help(parser, args.topic)

    try:
        if args.command == "init":
            repo = find_repo(Path.cwd())
            path = write_default_config(repo, overwrite=args.force)
            print(f"created {path}")
            return 0

        if args.command == "run":
            repo = find_repo(Path(args.repo).resolve() if args.repo else Path.cwd())
            config_root = Path(args.config_root).expanduser().resolve() if args.config_root else repo
            config = _override(load_config(config_root), args)
            return _run_once(repo, config, commit_sha=args.commit)
        if args.command == "watch":
            repo = find_repo(Path(args.repo).resolve() if args.repo else Path.cwd())
            config = _override(load_config(repo), args)
            print(f"watching {repo}")

            def on_change(sha: str) -> None:
                print(f"detected new HEAD {sha}; running review")
                _run_once(repo, config)

            watch_head(repo, config, on_change)
            return 0
        if args.command == "install-hook":
            repo = find_repo(Path(args.repo).resolve() if args.repo else Path.cwd())
            path = install_post_commit_hook(repo)
            print(f"installed {path}")
            return 0
        if args.command == "install-monitor":
            target = Path(args.repo).expanduser().resolve() if args.repo else Path.cwd()
            status = install_monitor(target)
            print(f"installed {status.plist_path}")
            print(f"running: {status.running}")
            return 0
        if args.command == "uninstall-monitor":
            target = Path(args.repo).expanduser().resolve() if args.repo else Path.cwd()
            status = uninstall_monitor(target)
            print(f"removed {status.plist_path}")
            return 0
        if args.command == "monitor-status":
            target = Path(args.repo).expanduser().resolve() if args.repo else Path.cwd()
            status = monitor_status(target)
            print(f"installed: {status.installed}")
            print(f"running: {status.running}")
            print(f"label: {status.label}")
            print(f"trace2: {status.trace2_target}")
            print(f"event log: {status.event_path}")
            print(f"launcher: {status.plist_path}")
            print(f"expected trace2: {status.expected_trace2_target}")
            return 0
        if args.command == "monitor":
            target = Path(args.repo).expanduser().resolve() if args.repo else None
            return run_monitor(target, once=args.once, poll_seconds=args.poll_interval)
        if args.command == "ui":
            target = Path(args.repo).expanduser().resolve() if args.repo else Path.cwd()
            serve_ui(
                target,
                host=args.host,
                port=args.port,
                open_browser=args.open,
            )
            return 0
    except (GitError, FileExistsError, ValueError) as exc:
        print(f"auto-ai-cr: {exc}", file=sys.stderr)
        return 1

    parser.print_help()
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="auto-ai-cr")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="show version and exit",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    command_parsers: dict[str, argparse.ArgumentParser] = {}

    init = subparsers.add_parser("init", help="create .auto-ai-cr.json")
    command_parsers["init"] = init
    init.add_argument("--force", action="store_true", help="overwrite existing config")

    run = subparsers.add_parser("run", help="run one review")
    command_parsers["run"] = run
    _add_common_args(run)

    watch = subparsers.add_parser("watch", help="watch HEAD changes and review commits")
    command_parsers["watch"] = watch
    _add_common_args(watch)

    hook = subparsers.add_parser("install-hook", help="install git post-commit hook")
    command_parsers["install-hook"] = hook
    hook.add_argument("--repo", help="repository path")

    install_monitor_parser = subparsers.add_parser(
        "install-monitor", help="install auto-ai-cr Trace2 daemon"
    )
    command_parsers["install-monitor"] = install_monitor_parser
    install_monitor_parser.add_argument("--repo", help="repository path")

    uninstall_monitor_parser = subparsers.add_parser(
        "uninstall-monitor", help="uninstall auto-ai-cr Trace2 daemon"
    )
    command_parsers["uninstall-monitor"] = uninstall_monitor_parser
    uninstall_monitor_parser.add_argument("--repo", help="repository path")

    monitor_status_parser = subparsers.add_parser(
        "monitor-status", help="show auto-ai-cr Trace2 daemon status"
    )
    command_parsers["monitor-status"] = monitor_status_parser
    monitor_status_parser.add_argument("--repo", help="repository path")

    monitor = subparsers.add_parser("monitor", help="run auto-ai-cr Trace2 daemon")
    command_parsers["monitor"] = monitor
    monitor.add_argument("--repo", help="repository path")
    monitor.add_argument("--once", action="store_true", help="scan once and exit")
    monitor.add_argument("--poll-interval", type=float, default=2.0)

    ui = subparsers.add_parser("ui", help="start the local configuration UI")
    command_parsers["ui"] = ui
    ui.add_argument("--repo", help="repository path")
    ui.add_argument("--host", default="127.0.0.1", help="bind host")
    ui.add_argument("--port", type=int, default=DEFAULT_PORT, help="bind port")
    ui.add_argument("--open", action="store_true", help="open browser automatically")

    help_parser = subparsers.add_parser("help", help="show help for auto-ai-cr or a command")
    help_parser.add_argument("topic", nargs="?", help="command name")
    command_parsers["help"] = help_parser
    setattr(parser, "command_parsers", command_parsers)
    return parser


def _print_help(parser: argparse.ArgumentParser, topic: str | None) -> int:
    command_parsers = getattr(parser, "command_parsers", {})
    if not topic:
        parser.print_help()
        return 0
    topic_parser = command_parsers.get(topic)
    if topic_parser is None:
        print(f"auto-ai-cr: unknown help topic: {topic}", file=sys.stderr)
        return 1
    topic_parser.print_help()
    return 0


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", help="repository path")
    parser.add_argument(
        "--scope",
        choices=["latest_commit", "branch_diff", "worktree", "staged"],
        help="review scope",
    )
    parser.add_argument("--base", help="base branch for branch_diff")
    parser.add_argument("--tool", help="review tool name from config")
    parser.add_argument("--commit", help="commit sha to review")
    parser.add_argument("--config-root", help="directory to load .auto-ai-cr.json from")


def _override(config: AppConfig, args: argparse.Namespace) -> AppConfig:
    return AppConfig(
        scope=getattr(args, "scope", None) or config.scope,
        base_branch=getattr(args, "base", None) or config.base_branch,
        tool=getattr(args, "tool", None) or config.tool,
        tools=config.tools,
        fix_tool=config.fix_tool,
        fix_tools=config.fix_tools,
        include=config.include,
        exclude=config.exclude,
        max_diff_chars=config.max_diff_chars,
        reports_dir=config.reports_dir,
        poll_interval_seconds=config.poll_interval_seconds,
        open_report_after_review=config.open_report_after_review,
        report_open_command=config.report_open_command,
        write_notes=config.write_notes,
        note_ref=config.note_ref,
    )


def _run_once(repo: Path, config: AppConfig, commit_sha: str | None = None) -> int:
    source = "daemon" if commit_sha else "manual"
    if commit_sha:
        record_review_started(repo, commit_sha, config.scope, source=source)
    diff_head_sha = commit_sha
    try:
        diff = collect_diff(
            repo,
            DiffRequest(
                scope=config.scope,
                base_branch=config.base_branch,
                include=config.include,
                exclude=config.exclude,
                max_diff_chars=config.max_diff_chars,
                commit_sha=commit_sha,
            ),
        )
        diff_head_sha = diff.head_sha
        if not commit_sha:
            record_review_started(repo, diff.head_sha, diff.scope, source=source)
        if not diff.diff.strip():
            print("no diff to review")
            record_review_finished(repo, diff.head_sha, "skipped", issue_count=0, exit_code=0)
            return 0
        result = run_review(repo, config, diff)
        print(f"review report: {result.report_path}")
        print(f"review issues: {result.issues_path}")
        record_review_finished(
            repo,
            diff.head_sha,
            "done" if result.exit_code == 0 else "failed",
            report_path=result.report_path,
            issues_path=result.issues_path,
            issue_count=len(result.issues),
            exit_code=result.exit_code,
        )
        return result.exit_code
    except Exception as exc:
        if diff_head_sha:
            record_review_finished(repo, diff_head_sha, "failed", error=str(exc))
        raise


if __name__ == "__main__":
    raise SystemExit(main())

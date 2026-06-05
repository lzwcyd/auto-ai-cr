from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .config import AppConfig, load_config, write_default_config
from .git_ops import DiffRequest, GitError, collect_diff, find_repo
from .hooks import install_post_commit_hook
from .reviewer import run_review
from .watcher import watch_head


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            repo = find_repo(Path.cwd())
            path = write_default_config(repo, overwrite=args.force)
            print(f"created {path}")
            return 0

        repo = find_repo(Path(args.repo).resolve() if args.repo else Path.cwd())
        config = _override(load_config(repo), args)

        if args.command == "run":
            return _run_once(repo, config)
        if args.command == "watch":
            print(f"watching {repo}")

            def on_change(sha: str) -> None:
                print(f"detected new HEAD {sha}; running review")
                _run_once(repo, config)

            watch_head(repo, config, on_change)
            return 0
        if args.command == "install-hook":
            path = install_post_commit_hook(repo)
            print(f"installed {path}")
            return 0
    except (GitError, FileExistsError, ValueError) as exc:
        print(f"auto-ai-cr: {exc}", file=sys.stderr)
        return 1

    parser.print_help()
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="auto-ai-cr")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="create .auto-ai-cr.json")
    init.add_argument("--force", action="store_true", help="overwrite existing config")

    run = subparsers.add_parser("run", help="run one review")
    _add_common_args(run)

    watch = subparsers.add_parser("watch", help="watch HEAD changes and review commits")
    _add_common_args(watch)

    hook = subparsers.add_parser("install-hook", help="install git post-commit hook")
    hook.add_argument("--repo", help="repository path")
    return parser


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", help="repository path")
    parser.add_argument(
        "--scope",
        choices=["latest_commit", "branch_diff", "worktree", "staged"],
        help="review scope",
    )
    parser.add_argument("--base", help="base branch for branch_diff")
    parser.add_argument("--tool", help="review tool name from config")


def _override(config: AppConfig, args: argparse.Namespace) -> AppConfig:
    return AppConfig(
        scope=getattr(args, "scope", None) or config.scope,
        base_branch=getattr(args, "base", None) or config.base_branch,
        tool=getattr(args, "tool", None) or config.tool,
        tools=config.tools,
        include=config.include,
        exclude=config.exclude,
        max_diff_chars=config.max_diff_chars,
        reports_dir=config.reports_dir,
        poll_interval_seconds=config.poll_interval_seconds,
    )


def _run_once(repo: Path, config: AppConfig) -> int:
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
        print("no diff to review")
        return 0
    result = run_review(repo, config, diff)
    print(f"review report: {result.report_path}")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())

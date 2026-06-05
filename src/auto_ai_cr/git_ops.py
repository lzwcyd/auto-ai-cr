from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess


VALID_SCOPES = {"latest_commit", "branch_diff", "worktree", "staged"}


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class DiffRequest:
    scope: str
    base_branch: str
    include: list[str]
    exclude: list[str]
    max_diff_chars: int
    commit_sha: str | None = None


@dataclass(frozen=True)
class DiffResult:
    scope: str
    base_branch: str
    head_sha: str
    subject: str
    diff: str
    truncated: bool


def run_git(repo: Path, args: list[str], check: bool = True) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and completed.returncode != 0:
        raise GitError(completed.stderr.strip() or completed.stdout.strip())
    return completed.stdout


def find_repo(start: Path) -> Path:
    output = run_git(start, ["rev-parse", "--show-toplevel"])
    return Path(output.strip()).resolve()


def try_find_repo(start: Path) -> Path | None:
    try:
        return find_repo(start)
    except Exception:
        return None


def head_sha(repo: Path) -> str:
    return run_git(repo, ["rev-parse", "HEAD"]).strip()


def current_branch(repo: Path) -> str:
    return run_git(repo, ["branch", "--show-current"]).strip() or "detached"


def _pathspec_args(include: list[str], exclude: list[str]) -> list[str]:
    args: list[str] = []
    if include or exclude:
        args.append("--")
        args.extend(include or [":/"])
        args.extend(f":(exclude){pattern}" for pattern in exclude)
    return args


def collect_diff(repo: Path, request: DiffRequest) -> DiffResult:
    if request.scope not in VALID_SCOPES:
        raise ValueError(f"unknown scope: {request.scope}")

    sha = head_sha(repo)
    if request.commit_sha:
        sha = run_git(repo, ["rev-parse", request.commit_sha]).strip()
    subject = _subject(repo, request.scope, request.base_branch, sha)
    pathspec = _pathspec_args(request.include, request.exclude)

    if request.scope == "latest_commit":
        args = [
            "show",
            "--find-renames",
            "--stat",
            "--patch",
            "--format=fuller",
            sha,
            *pathspec,
        ]
    elif request.scope == "branch_diff":
        args = [
            "diff",
            "--find-renames",
            "--stat",
            "--patch",
            f"{request.base_branch}...HEAD",
            *pathspec,
        ]
    elif request.scope == "staged":
        args = ["diff", "--cached", "--find-renames", "--stat", "--patch", *pathspec]
    else:
        args = ["diff", "--find-renames", "--stat", "--patch", *pathspec]

    diff = run_git(repo, args)
    truncated = len(diff) > request.max_diff_chars
    if truncated:
        diff = diff[: request.max_diff_chars] + "\n\n[diff truncated]\n"

    return DiffResult(
        scope=request.scope,
        base_branch=request.base_branch,
        head_sha=sha,
        subject=subject,
        diff=diff,
        truncated=truncated,
    )


def _subject(repo: Path, scope: str, base_branch: str, commit_sha: str) -> str:
    if scope == "latest_commit":
        return run_git(repo, ["log", "-1", "--pretty=%s", commit_sha]).strip()
    if scope == "branch_diff":
        branch = current_branch(repo)
        return f"{branch} vs {base_branch}"
    if scope == "staged":
        return "staged changes"
    return "worktree changes"

from auto_ai_cr.git_ops import DiffRequest, _pathspec_args


def test_pathspec_uses_repo_root_when_only_exclude_is_set():
    assert _pathspec_args([], ["*.lock"]) == ["--", ":/", ":(exclude)*.lock"]


def test_diff_request_accepts_branch_diff_scope():
    request = DiffRequest(
        scope="branch_diff",
        base_branch="master",
        include=[],
        exclude=[],
        max_diff_chars=100,
    )

    assert request.scope == "branch_diff"

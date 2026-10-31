"""Tests for the branch deletion algorithm in ``tool/branch_cleanup.py``.

The decision order must never change: these tests pin the current behaviour,
including the conservative cases (squash merge without merge-commit ancestry is
kept, unverifiable merge proof is kept).

Two layers are covered:

* pure ``evaluate_branch`` / ``evaluate_all_branches`` decisions;
* an end-to-end run of ``branch_cleanup.main()`` against real local git
  repositories with the GitHub API replaced by an in-process router (no
  network, no real deletion).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tool import branch_cleanup as bc
from tool.branch_cleanup import BranchInfo, EvalResult, evaluate_all_branches, evaluate_branch

# ---------------------------------------------------------------------------
# Pure decision logic
# ---------------------------------------------------------------------------


def evaluate(branch: BranchInfo | str, **kwargs) -> EvalResult:
    info = branch if isinstance(branch, BranchInfo) else BranchInfo(name=branch)
    defaults = dict(
        default_branch="main",
        open_pr_branches=set(),
        protected_branches=set(),
        is_ancestor=None,
        has_merged_pr=False,
        merge_commit_is_ancestor=None,
        diff_empty=None,
    )
    defaults.update(kwargs)
    return evaluate_branch(info, **defaults)


def test_default_branch_is_kept():
    result = evaluate("main", is_ancestor=True, diff_empty=True)
    assert result.action == "keep"
    assert result.reason == "default branch"


def test_protected_branch_flag_is_kept():
    result = evaluate(BranchInfo(name="release", protected=True), is_ancestor=True, diff_empty=True)
    assert result.action == "keep"
    assert result.reason == "protected branch"


def test_protected_branch_by_name_is_kept():
    result = evaluate(
        "release", protected_branches={"release"}, is_ancestor=True, diff_empty=True
    )
    assert result.action == "keep"
    assert result.reason == "protected branch"


def test_open_pull_request_is_kept():
    result = evaluate("feature", open_pr_branches={"feature"}, is_ancestor=True, diff_empty=True)
    assert result.action == "keep"
    assert result.reason == "open pull request"


def test_merged_ancestor_branch_is_deleted():
    result = evaluate("merged", is_ancestor=True, diff_empty=True)
    assert result.action == "delete"
    assert "ancestor of default" in result.reason


def test_unmerged_branch_is_kept():
    result = evaluate("feature", is_ancestor=False, has_merged_pr=False, diff_empty=False)
    assert result.action == "keep"
    assert result.reason.startswith("unmerged")


def test_unique_changes_win_over_ancestor_evidence():
    """A branch with unique changes is kept even if other evidence says merged."""
    result = evaluate("feature", is_ancestor=True, has_merged_pr=True, diff_empty=False)
    assert result.action == "keep"
    assert result.reason.startswith("unmerged")


def test_squash_merge_without_proof_is_kept():
    result = evaluate("squashed", is_ancestor=False, has_merged_pr=True, diff_empty=False)
    assert result.action == "keep"


def test_merged_pr_with_ancestor_merge_commit_is_deleted():
    result = evaluate(
        "squashed",
        is_ancestor=False,
        has_merged_pr=True,
        merge_commit_is_ancestor=True,
        diff_empty=None,
    )
    assert result.action == "delete"
    assert "merge commit is ancestor" in result.reason


def test_empty_diff_with_merged_pr_is_deleted():
    result = evaluate(
        "squashed",
        is_ancestor=False,
        has_merged_pr=True,
        merge_commit_is_ancestor=False,
        diff_empty=True,
    )
    assert result.action == "delete"
    assert "squash fallback" in result.reason


def test_unknown_ancestor_and_no_merged_pr_is_kept():
    result = evaluate("mystery", is_ancestor=None, has_merged_pr=False, diff_empty=None)
    assert result.action == "keep"
    assert result.reason.startswith("cannot prove merge")


def test_unknown_evidence_is_kept():
    result = evaluate("mystery", is_ancestor=False, has_merged_pr=True, diff_empty=None)
    assert result.action == "keep"
    assert result.reason.startswith("cannot prove complete merge")


def test_fetch_error_is_kept():
    result = evaluate("feature", fetch_error="network down", is_ancestor=True, diff_empty=True)
    assert result.action == "keep"
    assert result.reason.startswith("cannot verify safely")


def test_api_error_is_kept():
    result = evaluate("feature", api_error="403", is_ancestor=True, diff_empty=True)
    assert result.action == "keep"
    assert result.reason.startswith("cannot verify safely")


def test_fetch_error_has_priority_over_everything():
    result = evaluate(
        BranchInfo(name="main", protected=True),
        default_branch="main",
        fetch_error="boom",
    )
    assert result.action == "keep"


def test_evaluate_all_branches_matches_individual_evaluation():
    branches = [BranchInfo(name=name) for name in ("main", "merged", "feature", "protected")]
    evidence = {
        "merged": {"is_ancestor": True, "diff_empty": True},
        "feature": {"is_ancestor": False, "diff_empty": False},
        "protected": {"is_ancestor": True, "diff_empty": True},
    }
    batch = evaluate_all_branches(
        branches,
        default_branch="main",
        open_pr_branches=set(),
        protected_branches={"protected"},
        merge_evidence=evidence,
    )
    single = [
        evaluate(branch, protected_branches={"protected"} if branch.name == "protected" else set(),
                 **evidence.get(branch.name, {}))
        for branch in branches
    ]
    assert [(r.branch, r.action, r.reason) for r in batch] == [
        (r.branch, r.action, r.reason) for r in single
    ]


def test_multiple_repositories_do_not_change_results():
    """Running the algorithm per repository must equal running it standalone."""
    repo_a = [BranchInfo(name=name) for name in ("main", "a-merged", "a-open")]
    repo_b = [BranchInfo(name=name) for name in ("main", "b-merged", "b-unmerged")]
    evidence = {
        "a-merged": {"is_ancestor": True, "diff_empty": True},
        "a-open": {"is_ancestor": True, "diff_empty": True},
        "b-merged": {"is_ancestor": True, "diff_empty": True},
        "b-unmerged": {"is_ancestor": False, "diff_empty": False},
    }

    standalone_a = evaluate_all_branches(
        repo_a,
        default_branch="main",
        open_pr_branches={"a-open"},
        protected_branches=set(),
        merge_evidence=evidence,
    )
    standalone_b = evaluate_all_branches(
        repo_b,
        default_branch="main",
        open_pr_branches=set(),
        protected_branches=set(),
        merge_evidence=evidence,
    )

    combined: dict[str, list] = {}
    for name, branches, open_prs in (
        ("a", repo_a, {"a-open"}),
        ("b", repo_b, set()),
    ):
        combined[name] = evaluate_all_branches(
            branches,
            default_branch="main",
            open_pr_branches=open_prs,
            protected_branches=set(),
            merge_evidence=evidence,
        )

    assert [(r.branch, r.action) for r in combined["a"]] == [(r.branch, r.action) for r in standalone_a]
    assert [(r.branch, r.action) for r in combined["b"]] == [(r.branch, r.action) for r in standalone_b]
    # repository A's open PR must not affect repository B
    b_actions = {r.branch: r.action for r in combined["b"]}
    assert b_actions["b-merged"] == "delete"


# ---------------------------------------------------------------------------
# Real git repositories + fake GitHub API
# ---------------------------------------------------------------------------


GIT_ENV_BASE = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_TERMINAL_PROMPT": "0",
}


def git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    import os

    env = {**os.environ, **GIT_ENV_BASE}
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env=env)
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
    return proc


def commit(path: Path, filename: str, content: str, message: str | None = None) -> None:
    (path / filename).write_text(content, encoding="utf-8")
    git("add", "-A", cwd=path)
    git("commit", "-m", message or f"add {filename}", cwd=path)


class GitRepoFixture:
    """Builds a workspace clone whose ``origin`` is a local bare repository."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.upstream = root / "upstream.git"
        self.work = root / "work"
        self.clone = root / "clone"

    def build(self) -> "GitRepoFixture":
        self.root.mkdir(parents=True, exist_ok=True)
        git("init", "--bare", "--initial-branch=main", str(self.upstream), cwd=self.root)
        self.work.mkdir()
        git("init", "--initial-branch=main", cwd=self.work)
        commit(self.work, "base.txt", "base\n")
        git("remote", "add", "origin", str(self.upstream), cwd=self.work)
        git("push", "-u", "origin", "main", cwd=self.work)

        # merged branch: real merge commit, therefore an ancestor of main
        git("checkout", "-b", "merged", cwd=self.work)
        commit(self.work, "merged.txt", "merged\n")
        git("push", "origin", "merged", cwd=self.work)
        git("checkout", "main", cwd=self.work)
        git("merge", "--no-ff", "-m", "merge merged", "merged", cwd=self.work)
        git("push", "origin", "main", cwd=self.work)

        # unmerged branch: unique commit, diverged from main
        git("checkout", "-b", "unmerged", "HEAD", cwd=self.work)
        commit(self.work, "unmerged.txt", "unmerged\n")
        git("push", "origin", "unmerged", cwd=self.work)

        # squash-merged branch: same content on main, different commit
        git("checkout", "-b", "squashed", "main", cwd=self.work)
        commit(self.work, "squashed.txt", "squashed\n", message="feature: squashed work")
        git("push", "origin", "squashed", cwd=self.work)
        git("checkout", "main", cwd=self.work)
        commit(self.work, "squashed.txt", "squashed\n", message="squash merge: squashed work (#7)")
        git("push", "origin", "main", cwd=self.work)

        # protected branch (per API flag only)
        git("checkout", "-b", "protected", "main", cwd=self.work)
        commit(self.work, "protected.txt", "protected\n")
        git("push", "origin", "protected", cwd=self.work)
        git("checkout", "main", cwd=self.work)

        # branch with an open pull request
        git("checkout", "-b", "open-pr", "main", cwd=self.work)
        commit(self.work, "pr.txt", "pr\n")
        git("push", "origin", "open-pr", cwd=self.work)
        git("checkout", "main", cwd=self.work)

        # branch known only to the API (cannot be resolved by git)
        self.clone.mkdir()
        git("clone", "--no-checkout", str(self.upstream), str(self.clone), cwd=self.root)
        git("fetch", "origin", "+refs/heads/*:refs/remotes/origin/*", cwd=self.clone)
        return self


class ApiRouter:
    """In-process replacement for ``gh api`` used by branch_cleanup.run()."""

    def __init__(
        self,
        repository: str,
        *,
        branches: list[str],
        protected: set[str] | None = None,
        open_prs: set[str] | None = None,
        merged_prs: dict[str, dict] | None = None,
    ) -> None:
        self.repository = repository
        self.branches = list(branches)
        self.protected = protected or set()
        self.open_prs = open_prs or set()
        self.merged_prs = merged_prs or {}
        self.calls: list[tuple[str, str]] = []
        self.git_calls: list[list[str]] = []
        self.deletes: list[str] = []
        self.delete_failures: set[str] = set()

    def __call__(self, cmd, **kwargs):  # noqa: ANN001
        if cmd[0] != "gh":
            self.git_calls.append(list(cmd))
            return bc.subprocess.run(cmd, capture_output=True, text=True, **kwargs)

        method = "GET"
        if "--method" in cmd:
            method = cmd[cmd.index("--method") + 1]
        jq = None
        if "--jq" in cmd:
            jq = cmd[cmd.index("--jq") + 1]

        endpoint = None
        skip_next = False
        for index, token in enumerate(cmd[2:], start=2):
            if skip_next:
                skip_next = False
                continue
            if token in ("--method", "--jq", "-f", "--field", "--raw-field", "--header", "--input"):
                skip_next = True
                continue
            if token.startswith("-"):
                continue
            endpoint = token
            break

        assert endpoint is not None, f"no endpoint in {cmd}"
        self.calls.append((method, endpoint))

        def proc(returncode: int = 0, stdout: str = "", stderr: str = ""):
            return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

        if method == "DELETE":
            if endpoint.endswith("/protection"):
                return proc(1, "", "gh: Not Found (HTTP 404)")
            branch = endpoint.rsplit("/", 1)[-1]
            if branch in self.delete_failures:
                return proc(1, "", "gh: refusing to delete (HTTP 403)")
            self.deletes.append(branch)
            return proc(0)

        if endpoint == f"repos/{self.repository}":
            assert jq == ".default_branch"
            return proc(0, "main\n")

        if endpoint.startswith(f"repos/{self.repository}/branches/"):
            branch = endpoint.rsplit("/", 1)[-2]
            if branch in self.protected:
                return proc(0, '{"url": "protection"}\n')
            return proc(1, "", "gh: Not Found (HTTP 404)")

        if "/branches?" in endpoint:
            page = 1
            match = re.search(r"[?&]page=(\d+)", endpoint)
            if match:
                page = int(match.group(1))
            names = self.branches if page == 1 else []
            lines = [
                json.dumps(
                    {
                        "name": name,
                        "protected": name in self.protected,
                        "sha": "0" * 40,
                    }
                )
                for name in names
            ]
            return proc(0, "\n".join(lines))

        if "/pulls?" in endpoint:
            if "state=open" in endpoint:
                return proc(0, "\n".join(sorted(self.open_prs)))
            lines = [
                json.dumps(
                    {
                        "number": info["number"],
                        "head_ref": ref,
                        "state": "closed",
                        "merged": True,
                        "merged_at": "2026-01-01T00:00:00Z",
                        "merge_commit_sha": info.get("merge_commit_sha"),
                        "base_ref": "main",
                    }
                )
                for ref, info in self.merged_prs.items()
            ]
            return proc(0, "\n".join(lines))

        raise AssertionError(f"unexpected endpoint {method} {endpoint}")


def run_main(monkeypatch, router: ApiRouter, argv: list[str]) -> int:
    monkeypatch.setattr(bc, "run", router)
    monkeypatch.setattr(sys, "argv", ["branch_cleanup.py", *argv])
    try:
        bc.main()
    except SystemExit as exc:  # pragma: no cover - only on configuration errors
        return int(exc.code or 0)
    return 0


@pytest.fixture()
def git_fixture(tmp_path: Path, monkeypatch) -> GitRepoFixture:
    fixture = GitRepoFixture(tmp_path / "repo").build()
    monkeypatch.chdir(fixture.clone)
    return fixture


def decision_map(report_path: Path) -> dict[str, dict]:
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    return {item["branch"]: item for item in payload["results"]}


def test_end_to_end_dry_run_classifies_every_branch(git_fixture: GitRepoFixture, tmp_path, monkeypatch):
    branches = ["main", "merged", "unmerged", "squashed", "protected", "open-pr", "api-only"]
    router = ApiRouter(
        "owner/repo",
        branches=branches,
        protected={"protected"},
        open_prs={"open-pr"},
    )
    report_path = tmp_path / "branch-report.json"

    exit_code = run_main(
        monkeypatch,
        router,
        ["--repo", "owner/repo", "--skip-runs", "--json-report", str(report_path)],
    )

    assert exit_code == 0
    decisions = decision_map(report_path)

    assert decisions["main"]["reason"] == "default branch"
    assert decisions["protected"]["reason"] == "protected branch"
    assert decisions["open-pr"]["reason"] == "open pull request"
    assert decisions["merged"]["action"] == "delete"
    assert decisions["unmerged"]["action"] == "keep"
    assert decisions["unmerged"]["reason"].startswith("unmerged")
    # A squash merge keeps the branch: no merge-commit ancestry proof exists.
    assert decisions["squashed"]["action"] == "keep"
    # A branch the API knows but git cannot resolve is kept.
    assert decisions["api-only"]["action"] == "keep"
    assert decisions["api-only"]["reason"].startswith("cannot prove")

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["dry_run"] is True
    assert payload["deleted"] == 0
    assert payload["planned_deletions"] == 1
    assert router.deletes == []  # dry run never deletes


def test_end_to_end_live_deletes_only_the_merged_branch(git_fixture, tmp_path, monkeypatch):
    router = ApiRouter(
        "owner/repo",
        branches=["main", "merged", "unmerged"],
        merged_prs={},
    )
    report_path = tmp_path / "branch-report.json"

    exit_code = run_main(
        monkeypatch,
        router,
        ["--repo", "owner/repo", "--skip-runs", "--delete", "--json-report", str(report_path)],
    )

    assert exit_code == 0
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert router.deletes == ["merged"]
    assert payload["deleted"] == 1
    assert payload["planned_deletions"] == 1
    assert payload["mode"] == "LIVE"

    # Deletion goes through the API only — never through `git push`.
    assert router.deletes == ["merged"]
    assert not any(call[:2] == ["git", "push"] for call in router.git_calls)
    assert not any(call[:3] == ["git", "-C"] and "push" in call for call in router.git_calls)


def test_recheck_keeps_branch_that_moved_during_scan(git_fixture, tmp_path, monkeypatch):
    router = ApiRouter("owner/repo", branches=["main", "merged"])
    calls = {"count": 0}
    real_is_ancestor = bc.is_ancestor

    def flaky(branch, default_branch):
        calls["count"] += 1
        if calls["count"] == 1:
            return True
        return None  # the branch moved and can no longer be proven merged

    monkeypatch.setattr(bc, "is_ancestor", flaky)
    report_path = tmp_path / "branch-report.json"

    run_main(
        monkeypatch,
        router,
        ["--repo", "owner/repo", "--skip-runs", "--delete", "--json-report", str(report_path)],
    )

    decisions = decision_map(report_path)
    assert decisions["merged"]["action"] == "keep"
    assert "changed since evaluation" in decisions["merged"]["reason"]
    assert router.deletes == []
    monkeypatch.setattr(bc, "is_ancestor", real_is_ancestor)


def test_failed_deletion_is_reported_and_never_falls_back_to_git_push(
    git_fixture, tmp_path, monkeypatch
):
    router = ApiRouter("owner/repo", branches=["main", "merged"])
    router.delete_failures.add("merged")
    report_path = tmp_path / "branch-report.json"

    exit_code = run_main(
        monkeypatch,
        router,
        ["--repo", "owner/repo", "--skip-runs", "--delete", "--json-report", str(report_path)],
    )

    assert exit_code == 0  # legacy exit semantics preserved
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["failed"] == 1
    assert payload["deleted"] == 0
    assert router.deletes == []
    assert not any(call[:2] == ["git", "push"] for call in router.git_calls)


def test_protection_check_403_keeps_the_branch(git_fixture, tmp_path, monkeypatch):
    router = ApiRouter("owner/repo", branches=["main", "merged"])

    def deny_protection(cmd, **kwargs):
        if any("/protection" in str(part) for part in cmd) and "--method" not in cmd:
            return subprocess.CompletedProcess(
                cmd, 1, "", "gh: Resource not accessible (HTTP 403)"
            )
        return router(cmd, **kwargs)

    report_path = tmp_path / "branch-report.json"

    run_main(
        monkeypatch,
        deny_protection,
        ["--repo", "owner/repo", "--skip-runs", "--delete", "--json-report", str(report_path)],
    )

    decisions = decision_map(report_path)
    assert decisions["merged"]["action"] == "keep"
    assert "cannot verify protection" in decisions["merged"]["reason"]
    assert router.deletes == []


def test_report_contains_no_token(git_fixture, tmp_path, monkeypatch):
    token = "ghp_SUPERSECRETTOKENVALUE1234567890abcdef"
    monkeypatch.setenv("CLEANUP_TOKEN", token)
    router = ApiRouter("owner/repo", branches=["main", "merged"])
    report_path = tmp_path / "branch-report.json"

    run_main(
        monkeypatch,
        router,
        ["--repo", "owner/repo", "--skip-runs", "--json-report", str(report_path)],
    )

    assert token not in report_path.read_text(encoding="utf-8")

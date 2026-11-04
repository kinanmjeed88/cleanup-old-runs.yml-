"""Regression tests for branch discovery completeness and empty-repo proof.

These tests pin the fix for LIVE run #8: an empty repository must not be
confused with an incomplete listing, and a fallback must never overwrite a
successful API completeness result with ``bool(branches)``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tool import branch_cleanup as bc
from tool.branch_cleanup import BranchInfo, BranchListing
from tool.github_client import ApiResult
from tool.run_retention import RetentionOutcome, RetentionPlan, verify_after_execution


def completed(cmd, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)


def branch_line(name: str, protected: bool = False) -> str:
    return json.dumps({"name": name, "protected": protected, "sha": "a" * 40})


# ---------------------------------------------------------------------------
# Detailed API / git parsing
# ---------------------------------------------------------------------------


def test_api_nonempty_complete_does_not_call_git(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        assert cmd[0] == "gh"
        return completed(cmd, 0, branch_line("main") + "\n")

    monkeypatch.setattr(bc, "run", fake_run)
    listing = bc.discover_branches("owner/repo")
    assert listing.complete is True
    assert listing.source == "api"
    assert [b.name for b in listing.branches] == ["main"]
    assert all(cmd[0] == "gh" for cmd in calls)


def test_api_empty_and_git_empty_is_verified_empty(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[0] == "gh":
            return completed(cmd, 0, "")
        assert cmd[:3] == ["git", "ls-remote", "--heads"]
        return completed(cmd, 0, "")

    monkeypatch.setattr(bc, "run", fake_run)
    listing = bc.discover_branches("owner/repo")
    assert listing.complete is True
    assert listing.empty_verified is True
    assert listing.branches == []
    assert listing.source == "api+git"


def test_api_empty_but_git_failure_is_incomplete(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[0] == "gh":
            return completed(cmd, 0, "")
        return completed(cmd, 128, "", "fatal: could not read Username")

    monkeypatch.setattr(bc, "run", fake_run)
    listing = bc.discover_branches("owner/repo")
    assert listing.complete is False
    assert listing.empty_verified is False
    assert "could not verify emptiness" in listing.error


def test_api_empty_but_git_has_heads_uses_git(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[0] == "gh":
            return completed(cmd, 0, "")
        return completed(cmd, 0, f"{'b' * 40}\trefs/heads/main\n")

    monkeypatch.setattr(bc, "run", fake_run)
    listing = bc.discover_branches("owner/repo")
    assert listing.complete is True
    assert listing.source == "git"
    assert [b.name for b in listing.branches] == ["main"]
    assert listing.empty_verified is False


def test_api_failure_falls_back_to_complete_git(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[0] == "gh":
            return completed(cmd, 1, "", "gh: Forbidden (HTTP 403)")
        return completed(cmd, 0, f"{'c' * 40}\trefs/heads/topic\n")

    monkeypatch.setattr(bc, "run", fake_run)
    listing = bc.discover_branches("owner/repo")
    assert listing.complete is True
    assert listing.source == "git"
    assert listing.http_status is None or listing.returncode == 0
    assert [b.name for b in listing.branches] == ["topic"]


def test_api_failure_and_git_failure_is_incomplete(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[0] == "gh":
            return completed(cmd, 1, "", "gh: Server Error (HTTP 500)")
        return completed(cmd, 124, "", "command timed out after 120s")

    monkeypatch.setattr(bc, "run", fake_run)
    listing = bc.discover_branches("owner/repo")
    assert listing.complete is False
    assert listing.branches == []


def test_api_invalid_json_is_incomplete(monkeypatch):
    monkeypatch.setattr(bc, "run", lambda cmd, **kw: completed(cmd, 0, "not-json\n"))
    listing = bc._list_remote_branches_api_detailed("owner/repo")
    assert listing.complete is False
    assert "invalid branch record" in listing.error


def test_api_duplicate_branch_is_incomplete(monkeypatch):
    stdout = branch_line("main") + "\n" + branch_line("main") + "\n"
    monkeypatch.setattr(bc, "run", lambda cmd, **kw: completed(cmd, 0, stdout))
    listing = bc._list_remote_branches_api_detailed("owner/repo")
    assert listing.complete is False
    assert "duplicate" in listing.error


def test_git_invalid_sha_is_incomplete(monkeypatch):
    monkeypatch.setattr(
        bc, "run", lambda cmd, **kw: completed(cmd, 0, "zzz\trefs/heads/main\n")
    )
    listing = bc._list_remote_branches_git_detailed()
    assert listing.complete is False
    assert "invalid or duplicate" in listing.error


def test_git_unexpected_format_is_incomplete(monkeypatch):
    monkeypatch.setattr(bc, "run", lambda cmd, **kw: completed(cmd, 0, "main\n"))
    listing = bc._list_remote_branches_git_detailed()
    assert listing.complete is False


def test_listing_stderr_is_redacted(monkeypatch):
    secret = "ghp_EXAMPLEtokenVALUEforTESTINGonly1234567890"
    monkeypatch.setenv("CLEANUP_TOKEN", secret)

    def fake_run(cmd, **kwargs):
        if cmd[0] == "gh":
            return completed(cmd, 0, "")
        return completed(cmd, 128, "", f"fatal: authentication failed for {secret}")

    monkeypatch.setattr(bc, "run", fake_run)
    listing = bc.discover_branches("owner/repo")
    payload = listing.as_dict()
    assert secret not in json.dumps(payload)
    assert "REDACTED" in json.dumps(payload)
    assert any("REDACTED" in attempt.get("stderr", "") for attempt in payload["attempts"])


def test_run_timeout_returns_nonzero_completed_process(monkeypatch):
    def timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1))

    monkeypatch.setattr(bc.subprocess, "run", timeout)
    proc = bc.run(["git", "ls-remote", "--heads", "origin"], timeout=1)
    assert proc.returncode == 124
    assert "timed out" in proc.stderr


def test_refresh_remote_refs_uses_raw_refspec(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return completed(cmd, 0)

    monkeypatch.setattr(bc, "run", fake_run)
    assert bc.refresh_remote_refs("feature#1", "main") is True
    assert "+refs/heads/feature#1:refs/remotes/origin/feature#1" in captured["cmd"]
    assert "%23" not in " ".join(captured["cmd"])


# ---------------------------------------------------------------------------
# End-to-end main() against a real empty local git repository
# ---------------------------------------------------------------------------


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture()
def empty_clone(tmp_path, monkeypatch):
    root = tmp_path / "empty-repo"
    upstream = root / "upstream.git"
    clone = root / "clone"
    root.mkdir()
    _git("init", "--bare", str(upstream), cwd=root)
    _git("clone", "--no-checkout", str(upstream), str(clone), cwd=root)
    monkeypatch.chdir(clone)
    return clone


def _empty_api_router(*, git_ls_remote_fails=False):
    def router(cmd, **kwargs):
        if cmd[0] != "gh":
            if git_ls_remote_fails and cmd[:3] == ["git", "ls-remote", "--heads"]:
                return completed(cmd, 128, "", "fatal: repository not found")
            return subprocess.run(cmd, capture_output=True, text=True, **kwargs)
        endpoint = next(
            token
            for token in cmd[2:]
            if not token.startswith("-") and token not in {"GET", "DELETE"}
        )
        if endpoint == "repos/owner/repo":
            return completed(cmd, 0, "main\n")
        if "/branches?" in endpoint or "/pulls?" in endpoint:
            return completed(cmd, 0, "")
        raise AssertionError(f"unexpected endpoint {endpoint}")

    return router


def _run_main(monkeypatch, router, argv):
    monkeypatch.setattr(bc, "run", router)
    monkeypatch.setattr(sys, "argv", ["branch_cleanup.py", *argv])
    try:
        bc.main()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def test_main_empty_repository_succeeds_with_verified_empty(empty_clone, tmp_path, monkeypatch):
    report_path = tmp_path / "branch-report.json"
    exit_code = _run_main(
        monkeypatch,
        _empty_api_router(),
        ["--repo", "owner/repo", "--skip-runs", "--json-report", str(report_path)],
    )
    assert exit_code == 0
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["branches_examined"] == 0
    assert payload["deleted"] == 0
    assert payload["failed"] == 0
    assert payload["errors"] == []
    assert payload["empty_verified"] is True
    assert payload["branches_complete"] is True
    assert payload["branch_listing"]["source"] == "api+git"


def test_main_empty_api_with_git_failure_fails_closed(empty_clone, tmp_path, monkeypatch):
    report_path = tmp_path / "branch-report.json"
    exit_code = _run_main(
        monkeypatch,
        _empty_api_router(git_ls_remote_fails=True),
        ["--repo", "owner/repo", "--skip-runs", "--json-report", str(report_path)],
    )
    assert exit_code == 1
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["branches_complete"] is False
    assert payload["empty_verified"] is False
    assert payload["errors"]
    assert payload["branch_listing"]["complete"] is False


# ---------------------------------------------------------------------------
# Run verification: 404 is only already-absent after a complete re-list
# ---------------------------------------------------------------------------


class IncompleteVerificationClient:
    def list_workflow_runs(self, repo, page=1, per_page=100):
        return ApiResult(False, 500, None, "gh: Server Error (HTTP 500)")


def test_incomplete_relist_does_not_verify_404_as_absent():
    plan = RetentionPlan(repository="owner/repo", keep_latest=50)
    outcome = RetentionOutcome()
    outcome.already_absent.append(100000)
    outcome.resolved.append(100000)
    record = verify_after_execution(IncompleteVerificationClient(), "owner/repo", plan, outcome)
    assert record["listing_complete"] is False
    assert record["status"] == "incomplete"
    assert record["raw_delete_404_ids"] == [100000]
    assert record["verified_already_absent_ids"] == []


def test_gh_api_respects_retry_after(monkeypatch):
    calls = []
    sleeps = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if len(calls) == 1:
            return completed(cmd, 1, "", "gh: API rate limit exceeded (HTTP 429); Retry-After: 7")
        return completed(cmd, 0, '{"ok": true}')

    monkeypatch.setattr(bc, "run", fake_run)
    monkeypatch.setattr(bc.time, "sleep", lambda seconds: sleeps.append(seconds))
    assert bc.gh_api("repos/owner/repo") == {"ok": True}
    assert sleeps == [7]
    assert len(calls) == 2


def test_gh_api_plain_403_is_not_retried(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return completed(cmd, 1, "", "gh: Forbidden (HTTP 403)")

    monkeypatch.setattr(bc, "run", fake_run)
    monkeypatch.setattr(bc.time, "sleep", lambda seconds: pytest.fail("must not sleep on plain 403"))
    assert bc.gh_api("repos/owner/repo") is None
    assert len(calls) == 1

"""Regression tests for branch discovery completeness and empty-repo proof.

These tests pin the fix for LIVE run #8: an empty repository must not be
confused with an incomplete listing, and a fallback must never overwrite a
successful API completeness result with ``bool(branches)``.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys

import pytest

from tool import branch_cleanup as bc
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
    assert payload["open_pr_complete"] is True
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


# ---------------------------------------------------------------------------
# Pagination boundaries, PR listings, and fail-closed safety guards
# ---------------------------------------------------------------------------


def _api_pages(monkeypatch, pages: dict[int, str], *, fail_pages: set[int] | None = None):
    calls = []
    fail_pages = fail_pages or set()

    def fake_run(cmd, **kwargs):
        assert cmd[0] == "gh"
        endpoint = cmd[2]
        page = int(re.search(r"[?&]page=(\d+)", endpoint).group(1))
        calls.append(page)
        if page in fail_pages:
            return completed(cmd, 1, "", "gh: Server Error (HTTP 500)")
        return completed(cmd, 0, pages.get(page, ""))

    monkeypatch.setattr(bc, "run", fake_run)
    return calls


def test_api_pagination_boundary_short_page_is_complete(monkeypatch):
    monkeypatch.setattr(bc, "MAX_BRANCH_PAGES", 2)
    full = "\n".join(branch_line(f"b{i}") for i in range(100)) + "\n"
    calls = _api_pages(monkeypatch, {1: full, 2: branch_line("last") + "\n"})
    listing = bc._list_remote_branches_api_detailed("owner/repo")
    assert listing.complete is True
    assert listing.pages_read == 2
    assert len(listing.branches) == 101
    assert calls == [1, 2]


def test_api_pagination_cap_is_incomplete_not_complete(monkeypatch):
    monkeypatch.setattr(bc, "MAX_BRANCH_PAGES", 2)
    page1 = "\n".join(branch_line(f"b{i}") for i in range(100)) + "\n"
    page2 = "\n".join(branch_line(f"b{i}") for i in range(100, 200)) + "\n"
    calls = _api_pages(monkeypatch, {1: page1, 2: page2, 3: ""})
    listing = bc._list_remote_branches_api_detailed("owner/repo")
    assert listing.complete is False
    assert "safety cap" in listing.error
    assert calls == [1, 2]  # never guesses that page 3 would be empty


def test_api_invalid_sha_is_incomplete(monkeypatch):
    monkeypatch.setattr(
        bc,
        "run",
        lambda cmd, **kw: completed(cmd, 0, json.dumps({"name": "main", "protected": False, "sha": "zzz"}) + "\n"),
    )
    listing = bc._list_remote_branches_api_detailed("owner/repo")
    assert listing.complete is False
    assert "sha" in listing.error


def _open_pr_pages(monkeypatch, pages: dict[int, str]):
    def fake_run(cmd, **kwargs):
        assert "state=open" in cmd[2]
        page = int(re.search(r"[?&]page=(\d+)", cmd[2]).group(1))
        return completed(cmd, 0, pages.get(page, ""))

    monkeypatch.setattr(bc, "run", fake_run)


def test_open_pr_listing_counts_records_not_unique_branches(monkeypatch):
    page1 = "\n".join(json.dumps({"number": i, "head_ref": "shared"}) for i in range(1, 101)) + "\n"
    page2 = json.dumps({"number": 101, "head_ref": "other"}) + "\n"
    _open_pr_pages(monkeypatch, {1: page1, 2: page2})
    refs, complete = bc.list_open_pr_branches("owner/repo")
    assert complete is True
    assert refs == {"shared", "other"}


def test_open_pr_duplicate_number_is_incomplete(monkeypatch):
    page = json.dumps({"number": 1, "head_ref": "a"}) + "\n" + json.dumps({"number": 1, "head_ref": "b"}) + "\n"
    _open_pr_pages(monkeypatch, {1: page})
    refs, complete = bc.list_open_pr_branches("owner/repo")
    assert complete is False


def test_open_pr_invalid_head_ref_is_incomplete(monkeypatch):
    _open_pr_pages(monkeypatch, {1: json.dumps({"number": 1, "head_ref": None}) + "\n"})
    refs, complete = bc.list_open_pr_branches("owner/repo")
    assert complete is False


def test_closed_pr_pagination_counts_unmerged_records(monkeypatch):
    # A full page with only one merged PR must not stop pagination.
    page1_records = [{"number": i, "head_ref": f"unmerged-{i}", "state": "closed", "merged_at": None} for i in range(1, 100)]
    page1_records.append({"number": 100, "head_ref": "merged-old", "state": "closed", "merged_at": "2026-01-01T00:00:00Z", "merge_commit_sha": "d" * 40, "base_ref": "main"})
    page2_records = [{"number": 101, "head_ref": "merged-new", "state": "closed", "merged_at": "2026-01-02T00:00:00Z", "merge_commit_sha": "e" * 40, "base_ref": "main"}]
    pages = {
        1: "\n".join(json.dumps(r) for r in page1_records) + "\n",
        2: "\n".join(json.dumps(r) for r in page2_records) + "\n",
    }

    def fake_run(cmd, **kwargs):
        assert "state=closed" in cmd[2]
        page = int(re.search(r"[?&]page=(\d+)", cmd[2]).group(1))
        return completed(cmd, 0, pages.get(page, ""))

    monkeypatch.setattr(bc, "run", fake_run)
    prs = bc.list_closed_merged_prs("owner/repo")
    assert set(prs) == {"merged-old", "merged-new"}


def test_redaction_covers_inherited_tokens(monkeypatch):
    cleanup = "ghp_CLEANUPtokenVALUEforTESTINGonly1234567890"
    inherited = "ghp_INHERITEDtokenVALUEforTESTINGonly1234567890"
    monkeypatch.setenv("CLEANUP_TOKEN", cleanup)
    monkeypatch.setenv("GITHUB_TOKEN", inherited)

    def fake_run(cmd, **kwargs):
        if cmd[0] == "gh":
            return completed(cmd, 0, "")
        return completed(cmd, 128, "", f"fatal: {cleanup} / {inherited}")

    monkeypatch.setattr(bc, "run", fake_run)
    payload = bc.discover_branches("owner/repo").as_dict()
    rendered = json.dumps(payload)
    assert cleanup not in rendered
    assert inherited not in rendered
    assert rendered.count("REDACTED") >= 2


def test_subprocess_timeout_actually_stops_child():
    import time

    started = time.monotonic()
    proc = bc.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2)
    elapsed = time.monotonic() - started
    assert proc.returncode == 124
    assert elapsed < 2.0


def test_default_branch_fallback_preserves_slashes(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[0] == "gh":
            return completed(cmd, 1, "", "gh: Not Found (HTTP 404)")
        if cmd[:3] == ["git", "remote", "show"]:
            return completed(cmd, 0, "  HEAD branch: (unknown)\n")
        if cmd[:2] == ["git", "symbolic-ref"]:
            return completed(cmd, 0, "refs/remotes/origin/release/main\n")
        raise AssertionError(cmd)

    monkeypatch.setattr(bc, "run", fake_run)
    assert bc.fetch_default_branch("owner/repo") == "release/main"


def test_ensure_full_history_failure_is_fail_closed(monkeypatch):
    monkeypatch.setattr(bc, "run", lambda cmd, **kw: completed(cmd, 128, "", "fatal: fetch failed"))
    assert bc.ensure_full_history() is False


def test_protection_status_requires_explicit_http_404(monkeypatch):
    monkeypatch.setattr(bc, "run", lambda cmd, **kw: completed(cmd, 1, "", "upstream not found (HTTP 500)"))
    assert bc.protection_status("owner/repo", "topic") == "unknown"
    monkeypatch.setattr(bc, "run", lambda cmd, **kw: completed(cmd, 1, "", "gh: Branch not protected (HTTP 404)"))
    assert bc.protection_status("owner/repo", "topic") == "unprotected"


def test_branch_delete_404_requires_verified_absence(monkeypatch):
    monkeypatch.setattr(bc, "run", lambda cmd, **kw: completed(cmd, 1, "", "gh: Not Found (HTTP 404)"))
    monkeypatch.setattr(bc, "verify_branch_absent", lambda repo, branch: True)
    result = bc.delete_branch_remote_detailed("owner/repo", "topic", dry_run=False)
    assert result.outcome == "already_absent"
    assert result.verified_absent is True

    monkeypatch.setattr(bc, "verify_branch_absent", lambda repo, branch: False)
    result = bc.delete_branch_remote_detailed("owner/repo", "topic", dry_run=False)
    assert result.outcome == "failed"
    assert result.http_status == 404

    monkeypatch.setattr(bc, "verify_branch_absent", lambda repo, branch: None)
    result = bc.delete_branch_remote_detailed("owner/repo", "topic", dry_run=False)
    assert result.outcome == "failed"


def test_branch_delete_dry_run_never_calls_api(monkeypatch):
    def no_calls(cmd, **kwargs):
        raise AssertionError("dry-run must not call git/gh")

    monkeypatch.setattr(bc, "run", no_calls)
    result = bc.delete_branch_remote_detailed("owner/repo", "topic", dry_run=True)
    assert result.outcome == "planned"


def test_branch_delete_422_is_failed(monkeypatch):
    monkeypatch.setattr(bc, "run", lambda cmd, **kw: completed(cmd, 1, "", "gh: Unprocessable Entity (HTTP 422)"))
    result = bc.delete_branch_remote_detailed("owner/repo", "topic", dry_run=False)
    assert result.outcome == "failed"
    assert result.http_status == 422


def test_gh_api_retry_after_is_capped(monkeypatch):
    sleeps = []
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if len(calls) == 1:
            return completed(cmd, 1, "", "gh: rate limit (HTTP 429); Retry-After: 999")
        return completed(cmd, 0, "{}")

    monkeypatch.setattr(bc, "run", fake_run)
    monkeypatch.setattr(bc.time, "sleep", sleeps.append)
    assert bc.gh_api("repos/owner/repo") == {}
    assert sleeps == [bc.MAX_RETRY_WAIT_SECONDS]


def test_legacy_run_cleanup_incomplete_listing_never_deletes(monkeypatch):
    calls = []
    old = "2000-01-01T00:00:00Z"
    page1 = json.dumps([{"id": i, "created_at": old, "status": "completed", "name": f"r{i}"} for i in range(100)])

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "--method" in cmd and "DELETE" in cmd:
            raise AssertionError("incomplete legacy listing must not delete")
        page = int(re.search(r"[?&]page=(\d+)", cmd[2]).group(1))
        if page == 1:
            return completed(cmd, 0, page1)
        return completed(cmd, 1, "", "gh: Server Error (HTTP 500)")

    monkeypatch.setattr(bc, "run", fake_run)
    deleted, skipped, failed = bc.cleanup_runs("owner/repo", cutoff_hours=1, dry_run=False)
    assert deleted == 0
    assert failed == 1

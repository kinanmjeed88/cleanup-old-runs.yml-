"""Reliability, pagination-snapshot, transport and verification regressions."""

from __future__ import annotations

import subprocess

import pytest

from tests.fakes import FakeClient, build_runs
from tool.github_client import ApiResult, GhCliClient, GhUnavailableError
from tool.run_retention import (
    apply_retention,
    fetch_all_runs,
    plan_retention,
    verify_after_execution,
)


def test_total_count_drift_during_pagination_fails_closed():
    client = FakeClient()
    client.add_repository("owner/repo", runs=build_runs(120))
    original = client.list_workflow_runs

    def drifting(repo, page=1, per_page=100):
        result = original(repo, page=page, per_page=per_page)
        if page == 2 and isinstance(result.data, dict):
            result.data["total_count"] = 121
        return result

    client.list_workflow_runs = drifting  # type: ignore[assignment]
    listing = fetch_all_runs(client, "owner/repo")
    assert listing.complete is False
    assert any("total_count changed" in error for error in listing.errors)
    plan = plan_retention(listing.runs, repository="owner/repo", complete=listing.complete, errors=listing.errors)
    assert plan.aborted and plan.delete == []


def test_duplicate_id_across_pages_fails_closed():
    client = FakeClient()
    client.add_repository("owner/repo", runs=build_runs(120))
    original = client.list_workflow_runs

    def duplicate(repo, page=1, per_page=100):
        result = original(repo, page=page, per_page=per_page)
        if page == 2 and isinstance(result.data, dict):
            result.data["workflow_runs"][0] = dict(result.data["workflow_runs"][0])
            # Repeat an id from page 1 (newest-first listing).
            result.data["workflow_runs"][0]["id"] = 1000
        return result

    client.list_workflow_runs = duplicate  # type: ignore[assignment]
    listing = fetch_all_runs(client, "owner/repo")
    assert listing.complete is False
    assert 1000 in listing.duplicate_ids


def test_collected_more_than_total_count_fails_closed():
    client = FakeClient()
    client.add_repository("owner/repo", runs=build_runs(100))
    original = client.list_workflow_runs

    def bad_total(repo, page=1, per_page=100):
        result = original(repo, page=page, per_page=per_page)
        if isinstance(result.data, dict):
            result.data["total_count"] = 50
        return result

    client.list_workflow_runs = bad_total  # type: ignore[assignment]
    listing = fetch_all_runs(client, "owner/repo")
    assert listing.complete is False
    assert any("total_count=50" in error for error in listing.errors)


def test_missing_total_count_fails_closed():
    client = FakeClient()
    client.add_repository("owner/repo", runs=build_runs(10))
    original = client.list_workflow_runs

    def no_total(repo, page=1, per_page=100):
        result = original(repo, page=page, per_page=per_page)
        if isinstance(result.data, dict):
            result.data.pop("total_count", None)
        return result

    client.list_workflow_runs = no_total  # type: ignore[assignment]
    listing = fetch_all_runs(client, "owner/repo")
    assert listing.complete is False


def test_active_runs_are_counted_and_never_deleted():
    runs = build_runs(60)
    runs += [
        {"id": 9001, "created_at": "2026-01-01T00:00:00Z", "status": "in_progress"},
        {"id": 9002, "created_at": "2026-01-01T00:01:00Z", "status": "queued"},
    ]
    from tool.run_retention import WorkflowRun

    plan = plan_retention([WorkflowRun.from_api(r) for r in runs], repository="owner/repo")
    assert plan.completed_count == 60
    assert plan.active_count == 2
    assert len(plan.keep) == 50
    assert len(plan.delete) == 10
    assert {9001, 9002}.isdisjoint({r.id for r in plan.delete})


def test_deletion_candidates_are_oldest_first():
    from tool.run_retention import WorkflowRun

    plan = plan_retention([WorkflowRun.from_api(r) for r in build_runs(60)], repository="owner/repo")
    ids = [r.id for r in plan.delete]
    assert ids == sorted(ids)
    assert ids[0] == 1000


def test_404_is_already_absent_not_a_failure():
    client = FakeClient()
    client.add_repository("owner/repo", runs=build_runs(55))
    listing = fetch_all_runs(client, "owner/repo")
    plan = plan_retention(listing.runs, repository="owner/repo", complete=listing.complete)
    first = plan.delete[0].id
    client.runs["owner/repo"] = [r for r in client.runs["owner/repo"] if r["id"] != first]

    outcome = apply_retention(client, "owner/repo", plan, dry_run=False, log=lambda _m: None)
    assert first in outcome.already_absent
    assert first not in outcome.deleted
    assert outcome.failed == []
    assert len(outcome.deleted) == 4


def test_server_error_stops_repository():
    client = FakeClient()
    client.add_repository("owner/repo", runs=build_runs(55))
    listing = fetch_all_runs(client, "owner/repo")
    plan = plan_retention(listing.runs, repository="owner/repo", complete=listing.complete)

    def fail(repo, run_id):
        return ApiResult(False, 500, None, "gh: Server Error (HTTP 500)")

    client.delete_workflow_run = fail  # type: ignore[assignment]
    outcome = apply_retention(client, "owner/repo", plan, dry_run=False, log=lambda _m: None)
    assert outcome.deleted == []
    assert len(outcome.failed) == 1
    assert outcome.stop_reason and "HTTP 500" in outcome.stop_reason


def test_transport_timeout_is_retried_then_returned_as_failure(token):
    calls = {"n": 0}
    sleeps = []

    def runner(cmd, env, timeout):
        calls["n"] += 1
        raise GhUnavailableError("`gh` timed out after 60s")

    client = GhCliClient(token=token, runner=runner, sleep=sleeps.append, log=lambda _m: None, max_retries=2)
    result = client.api("repos/o/r")
    assert result.ok is False
    assert result.transport_error
    assert calls["n"] == 3
    assert sleeps == [1, 2]


def test_missing_gh_is_not_retried(token):
    calls = {"n": 0}

    def runner(cmd, env, timeout):
        calls["n"] += 1
        raise GhUnavailableError("the `gh` CLI is required but was not found on PATH")

    client = GhCliClient(token=token, runner=runner, sleep=lambda _s: None, log=lambda _m: None)
    result = client.api("repos/o/r")
    assert result.ok is False
    assert calls["n"] == 1


def test_retry_after_header_is_honoured(token):
    calls = {"n": 0}
    sleeps = []

    def runner(cmd, env, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            return subprocess.CompletedProcess(cmd, 1, "", "gh: rate limit exceeded (HTTP 429)\nRetry-After: 7\n")
        return subprocess.CompletedProcess(cmd, 0, '{"ok": true}', "")

    client = GhCliClient(token=token, runner=runner, sleep=sleeps.append, log=lambda _m: None)
    result = client.api("repos/o/r")
    assert result.ok is True
    assert sleeps == [7]


def test_plain_403_is_not_retried(token):
    calls = {"n": 0}

    def runner(cmd, env, timeout):
        calls["n"] += 1
        return subprocess.CompletedProcess(cmd, 1, "", "gh: Resource not accessible by integration (HTTP 403)")

    client = GhCliClient(token=token, runner=runner, sleep=lambda _s: None, log=lambda _m: None)
    result = client.api("repos/o/r")
    assert result.ok is False
    assert result.status == 403
    assert calls["n"] == 1


def test_verification_detects_still_present_resolved_id():
    client = FakeClient()
    client.add_repository("owner/repo", runs=build_runs(55))
    listing = fetch_all_runs(client, "owner/repo")
    plan = plan_retention(listing.runs, repository="owner/repo", complete=listing.complete)
    from tool.run_retention import RetentionOutcome

    outcome = RetentionOutcome(deleted=[plan.delete[0].id], planned=[r.id for r in plan.delete])
    # Fake API still contains the run: verification must flag the mismatch.
    record = verify_after_execution(client, "owner/repo", plan, outcome)
    assert record["status"] == "mismatch"
    assert plan.delete[0].id in record["still_present_resolved_ids"]


def test_verification_incomplete_listing_is_not_verified():
    client = FakeClient()
    client.add_repository("owner/repo", runs=build_runs(120))
    listing = fetch_all_runs(client, "owner/repo")
    plan = plan_retention(listing.runs, repository="owner/repo", complete=listing.complete)
    client.fail_runs_page["owner/repo"] = 2
    from tool.run_retention import RetentionOutcome

    record = verify_after_execution(client, "owner/repo", plan, RetentionOutcome())
    assert record["status"] == "incomplete"
    assert record["listing_complete"] is False


def test_unverified_404_does_not_make_a_failed_repository_partial():
    from tool.reports import RepoReport

    report = RepoReport(repository="owner/repo", mode="LIVE")
    report.runs_already_absent = 1
    report.verification = {"status": "mismatch", "still_present_resolved_ids": [1000]}
    report.add_failure("post-execution verification mismatch: run 1000 is still present")

    report.finalise_status(live=True, executed_any_phase=True)

    assert report.status == "FAILED"

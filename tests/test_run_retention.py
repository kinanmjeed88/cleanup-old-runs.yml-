"""Retention policy tests: keep the newest 50 completed runs per repository."""

from __future__ import annotations

import pytest

from tests.fakes import FakeClient, api_run, build_runs, iso
from tool.github_client import ApiResult
from tool.run_retention import (
    DEFAULT_KEEP_LATEST,
    REASON_CURRENT_RUN,
    REASON_UNVERIFIABLE_CREATED_AT,
    REASON_UNVERIFIABLE_ID,
    REASON_UNVERIFIABLE_STATUS,
    WorkflowRun,
    apply_retention,
    fetch_all_runs,
    plan_retention,
)


def make_runs(count: int, **kwargs) -> list[WorkflowRun]:
    return [WorkflowRun.from_api(payload) for payload in build_runs(count, **kwargs)]


def test_default_retention_is_fifty():
    assert DEFAULT_KEEP_LATEST == 50


def test_one_hundred_completed_runs_keep_fifty_delete_fifty():
    runs = make_runs(100)
    plan = plan_retention(runs, repository="owner/repo")

    assert plan.complete is True
    assert not plan.aborted
    assert len(plan.keep) == 50
    assert len(plan.delete) == 50
    assert plan.examined == 100
    # The retained set is exactly the 50 newest runs.
    assert {run.id for run in plan.keep} == set(range(1050, 1100))
    assert {run.id for run in plan.delete} == set(range(1000, 1050))


def test_exactly_fifty_runs_are_all_kept():
    plan = plan_retention(make_runs(50), repository="owner/repo")

    assert len(plan.keep) == 50
    assert plan.delete == []


def test_fewer_than_fifty_runs_are_all_kept():
    plan = plan_retention(make_runs(8), repository="owner/repo")

    assert len(plan.keep) == 8
    assert plan.delete == []


def test_zero_runs_is_a_noop():
    plan = plan_retention([], repository="owner/repo")

    assert plan.keep == []
    assert plan.delete == []
    assert plan.examined == 0


def test_active_runs_are_never_deleted():
    completed = make_runs(60)
    active = [
        WorkflowRun(id=5001, created_at=iso(1), status="in_progress"),
        WorkflowRun(id=5002, created_at=iso(2), status="queued"),
        WorkflowRun(id=5003, created_at=iso(3), status="waiting"),
        WorkflowRun(id=5004, created_at=iso(4), status="requested"),
        WorkflowRun(id=5005, created_at=iso(5), status="pending"),
    ]
    plan = plan_retention(completed + active, repository="owner/repo")

    deleted_ids = {run.id for run in plan.delete}
    assert deleted_ids.isdisjoint({run.id for run in active})
    assert len(plan.keep) == 50
    assert len(plan.delete) == 10
    reasons = {item.run.id: item.reason for item in plan.excluded}
    assert reasons[5002] == "active: queued"
    assert reasons[5001] == "active: in_progress"


def test_current_run_is_never_deleted_and_does_not_consume_a_slot():
    runs = make_runs(55)
    current = runs[-1].id  # newest run == the cleanup run itself
    plan = plan_retention(runs, repository="owner/repo", current_run_id=str(current))

    assert current not in {run.id for run in plan.delete}
    assert current not in {run.id for run in plan.keep}
    assert len(plan.keep) == 50
    assert len(plan.delete) == 4
    assert any(item.run.id == current and item.reason == REASON_CURRENT_RUN for item in plan.excluded)


def test_unverifiable_created_at_is_never_deleted():
    runs = make_runs(25)
    runs[0] = WorkflowRun(id=runs[0].id, created_at="not-a-timestamp", status="completed")
    plan = plan_retention(runs, repository="owner/repo")

    assert runs[0].id not in {run.id for run in plan.delete}
    assert any(item.reason == REASON_UNVERIFIABLE_CREATED_AT for item in plan.excluded)


def test_unknown_status_is_never_deleted():
    runs = make_runs(25)
    runs[0] = WorkflowRun(id=runs[0].id, created_at=runs[0].created_at, status="teleported")
    plan = plan_retention(runs, repository="owner/repo")

    assert runs[0].id not in {run.id for run in plan.delete}
    assert any(item.reason == REASON_UNVERIFIABLE_STATUS for item in plan.excluded)


def test_run_without_id_is_never_deleted():
    runs = make_runs(25) + [WorkflowRun(id=None, created_at=iso(999), status="completed")]
    plan = plan_retention(runs, repository="owner/repo")

    assert all(run.id is not None for run in plan.delete)
    assert any(item.reason == REASON_UNVERIFIABLE_ID for item in plan.excluded)


def test_incomplete_listing_deletes_nothing():
    runs = make_runs(100)
    plan = plan_retention(runs, repository="owner/repo", complete=False, errors=["page 2 failed"])

    assert plan.aborted
    assert plan.delete == []
    assert len(plan.keep) == 100


def test_invalid_keep_latest_fails_closed():
    runs = make_runs(50)
    plan = plan_retention(runs, repository="owner/repo", keep_latest=0)

    assert plan.aborted
    assert plan.delete == []


def test_ordering_is_strictly_by_created_at_then_id():
    runs = [
        WorkflowRun(id=3, created_at=iso(10), status="completed"),
        WorkflowRun(id=1, created_at=iso(10), status="completed"),  # same timestamp, lower id
        WorkflowRun(id=2, created_at=iso(20), status="completed"),
    ]
    plan = plan_retention(runs, repository="owner/repo", keep_latest=1)

    assert [run.id for run in plan.keep] == [2]
    assert {run.id for run in plan.delete} == {1, 3}


def test_duplicate_ids_are_ignored():
    runs = make_runs(51)
    duplicates = [WorkflowRun(id=runs[-1].id, created_at=runs[-1].created_at, status="completed")]
    plan = plan_retention(runs + duplicates, repository="owner/repo")

    assert len(plan.keep) == 50
    assert len(plan.delete) == 1
    assert any(item.reason.startswith("duplicate") for item in plan.excluded)


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def test_pagination_reads_every_page():
    client = FakeClient()
    client.add_repository("owner/repo", runs=build_runs(250))
    listing = fetch_all_runs(client, "owner/repo")

    assert listing.complete is True
    assert len(listing.runs) == 250
    assert listing.reported_total == 250
    assert [page for _, page in client.list_calls] == [1, 2, 3]


def test_more_than_one_hundred_runs_are_all_considered():
    client = FakeClient()
    client.add_repository("owner/repo", runs=build_runs(140))
    listing = fetch_all_runs(client, "owner/repo")
    plan = plan_retention(listing.runs, repository="owner/repo", complete=listing.complete)

    assert plan.examined == 140
    assert len(plan.keep) == 50
    assert len(plan.delete) == 90


def test_page_failure_aborts_the_listing():
    client = FakeClient()
    client.add_repository("owner/repo", runs=build_runs(250))
    client.fail_runs_page["owner/repo"] = 2
    listing = fetch_all_runs(client, "owner/repo")

    assert listing.complete is False
    assert listing.errors
    plan = plan_retention(
        listing.runs, repository="owner/repo", complete=listing.complete, errors=listing.errors
    )
    assert plan.aborted
    assert plan.delete == []


def test_total_count_mismatch_aborts_the_listing():
    client = FakeClient()
    client.add_repository("owner/repo", runs=build_runs(50))
    original = client.list_workflow_runs

    def truncated(repo, page=1, per_page=100):
        result = original(repo, page=page, per_page=per_page)
        if page == 1 and isinstance(result.data, dict):
            result.data["total_count"] = 120  # API claims more than we can see
        return result

    client.list_workflow_runs = truncated  # type: ignore[assignment]
    listing = fetch_all_runs(client, "owner/repo")

    assert listing.complete is False
    assert any("total_count" in error for error in listing.errors)


def test_malformed_run_entry_aborts_the_listing():
    client = FakeClient()
    client.add_repository("owner/repo", runs=build_runs(25))
    client.runs["owner/repo"][0] = {"created_at": iso(1), "status": "completed"}  # no id

    listing = fetch_all_runs(client, "owner/repo")
    assert listing.complete is False
    plan = plan_retention(
        listing.runs, repository="owner/repo", complete=listing.complete, errors=listing.errors
    )
    assert plan.delete == []


def test_actions_read_failure_yields_no_deletion():
    client = FakeClient()
    client.add_repository("owner/repo", runs=build_runs(60))
    client.forbid_runs.add("owner/repo")
    listing = fetch_all_runs(client, "owner/repo")
    plan = plan_retention(
        listing.runs, repository="owner/repo", complete=listing.complete, errors=listing.errors
    )

    outcome = apply_retention(client, "owner/repo", plan, dry_run=False)
    assert outcome.deleted == []
    assert client.delete_calls == []


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def test_dry_run_never_calls_delete():
    client = FakeClient()
    client.add_repository("owner/repo", runs=build_runs(75))
    listing = fetch_all_runs(client, "owner/repo")
    plan = plan_retention(listing.runs, repository="owner/repo", complete=listing.complete)

    outcome = apply_retention(client, "owner/repo", plan, dry_run=True, log=lambda _msg: None)

    assert outcome.deleted == []
    assert len(outcome.planned) == 25
    assert client.delete_calls == []


def test_live_run_deletes_exactly_the_planned_runs():
    client = FakeClient()
    client.add_repository("owner/repo", runs=build_runs(75))
    listing = fetch_all_runs(client, "owner/repo")
    plan = plan_retention(listing.runs, repository="owner/repo", complete=listing.complete)

    outcome = apply_retention(client, "owner/repo", plan, dry_run=False, log=lambda _msg: None)

    assert len(outcome.deleted) == 25
    assert outcome.failed == []
    assert len(client.runs["owner/repo"]) == 50
    assert {run["id"] for run in client.runs["owner/repo"]} == set(range(1025, 1075))


def test_permission_error_stops_further_deletions():
    client = FakeClient()
    client.add_repository("owner/repo", runs=build_runs(60))
    client.forbid_delete.add("owner/repo")
    listing = fetch_all_runs(client, "owner/repo")
    plan = plan_retention(listing.runs, repository="owner/repo", complete=listing.complete)

    messages: list[str] = []
    outcome = apply_retention(client, "owner/repo", plan, dry_run=False, log=messages.append)

    assert outcome.deleted == []
    assert outcome.stop_reason is not None and "Actions: Read and write" in outcome.stop_reason
    assert len(outcome.failed) == 1
    assert len(client.delete_calls) == 1  # abandoned after the first refusal


def test_first_delete_401_reports_actual_status_and_stops():
    client = FakeClient()
    client.add_repository("owner/repo", runs=build_runs(60))
    listing = fetch_all_runs(client, "owner/repo")
    plan = plan_retention(listing.runs, repository="owner/repo", complete=listing.complete)
    calls = []

    def unauthorized(repo, run_id):
        calls.append((repo, run_id))
        return ApiResult(False, 401, None, "gh: Bad credentials (HTTP 401)")

    client.delete_workflow_run = unauthorized  # type: ignore[assignment]
    outcome = apply_retention(client, "owner/repo", plan, dry_run=False, log=lambda _msg: None)

    assert outcome.deleted == []
    assert len(outcome.failed) == 1
    assert len(calls) == 1
    assert "HTTP 401" in outcome.stop_reason
    assert "Actions: Read and write" in outcome.stop_reason


def test_422_failed_deletion_is_reported_but_processing_continues():
    client = FakeClient()
    client.add_repository("owner/repo", runs=build_runs(53))
    listing = fetch_all_runs(client, "owner/repo")
    plan = plan_retention(listing.runs, repository="owner/repo", complete=listing.complete)

    calls = {"n": 0}
    original = client.delete_workflow_run

    def flaky(repo, run_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return type("R", (), {"ok": False, "status": 422, "error": "boom", "forbidden": False, "not_found": False})()
        return original(repo, run_id)

    client.delete_workflow_run = flaky  # type: ignore[assignment]
    outcome = apply_retention(client, "owner/repo", plan, dry_run=False, log=lambda _msg: None)

    assert len(outcome.failed) == 1
    assert len(outcome.deleted) == 2


def test_multiple_repositories_use_independent_counters():
    """Repository A 75 runs, B 52, C 8 — each keeps its own newest 50."""
    client = FakeClient()
    client.add_repository("owner/a", runs=build_runs(75, first_id=1000))
    client.add_repository("owner/b", runs=build_runs(52, first_id=2000))
    client.add_repository("owner/c", runs=build_runs(8, first_id=3000))

    plans = {}
    for repo in ("owner/a", "owner/b", "owner/c"):
        listing = fetch_all_runs(client, repo)
        plans[repo] = plan_retention(listing.runs, repository=repo, complete=listing.complete)

    assert len(plans["owner/a"].keep) == 50
    assert len(plans["owner/a"].delete) == 25
    assert len(plans["owner/b"].keep) == 50
    assert len(plans["owner/b"].delete) == 2
    assert len(plans["owner/c"].keep) == 8
    assert len(plans["owner/c"].delete) == 0

    # No cross-repository mixing: deletion ids stay inside their own repository.
    a_ids = {run.id for run in plans["owner/a"].delete}
    b_ids = {run.id for run in plans["owner/b"].delete}
    assert a_ids and b_ids and a_ids.isdisjoint(b_ids)


def test_priority_is_count_not_age():
    """80 runs all older than a year: the newest 50 are still kept."""
    old_runs = [
        api_run(1000 + index, f"2024-{1 + index // 28:02d}-{1 + index % 28:02d}T00:00:00Z")
        for index in range(80)
    ]
    plan = plan_retention([WorkflowRun.from_api(run) for run in old_runs], repository="owner/repo")

    assert len(plan.keep) == 50
    assert len(plan.delete) == 30


@pytest.mark.parametrize("keep", [1, 5, 20, 50])
def test_configurable_retention_count(keep):
    plan = plan_retention(make_runs(80), repository="owner/repo", keep_latest=keep)
    assert len(plan.keep) == keep
    assert len(plan.delete) == 80 - keep


def test_eighteen_completed_runs_are_all_retained_with_no_candidates():
    runs = make_runs(18, first_id=300000)
    plan = plan_retention(runs, repository="central/repo", keep_latest=50, current_run_id="999999")

    assert plan.completed_count == 18
    assert {run.id for run in plan.keep} == {run.id for run in runs}
    assert plan.delete == []
    assert plan.excluded == []
    assert plan.aborted is False


@pytest.mark.parametrize("status", [501, 505, 599])
def test_any_5xx_stops_repository_deletions(status):
    client = FakeClient()
    client.add_repository("owner/repo", runs=build_runs(55))
    listing = fetch_all_runs(client, "owner/repo")
    plan = plan_retention(listing.runs, repository="owner/repo", complete=listing.complete)

    def fail(repo, run_id):
        return ApiResult(False, status, None, f"gh: injected server error (HTTP {status})")

    client.delete_workflow_run = fail  # type: ignore[assignment]
    outcome = apply_retention(client, "owner/repo", plan, dry_run=False, log=lambda _m: None)
    assert outcome.deleted == []
    assert len(outcome.failed) == 1
    assert outcome.stop_reason and f"HTTP {status}" in outcome.stop_reason
    assert len(outcome.unattempted) == len(plan.delete) - 1

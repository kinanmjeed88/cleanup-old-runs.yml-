"""Orchestrator tests: multi-repository isolation, permissions, safety gates."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.fakes import FakeBranchRunner, FakeClient, FakeWorkspaceFactory, build_runs
from tool import central_cleanup
from tool.central_cleanup import (
    EXIT_CONFIG,
    EXIT_FAILURES,
    EXIT_OK,
    CentralCleanup,
    CleanupOptions,
    check_repository_access,
    main,
)
from tool.git_workspace import WorkspaceError
from tool.reports import ReportWriter


@pytest.fixture()
def writer(tmp_path: Path) -> ReportWriter:
    return ReportWriter(tmp_path / "diagnostics", mode="DRY RUN")


def make_cleanup(client, tmp_path, *, options=None, branch_runner=None, workspace_factory=None, writer=None):
    options = options or CleanupOptions(mode="dry-run", keep_latest=20)
    return CentralCleanup(
        client=client,
        workspace_factory=workspace_factory
        or FakeWorkspaceFactory(tmp_path / "workspaces"),
        branch_runner=branch_runner or FakeBranchRunner(),
        writer=writer or ReportWriter(tmp_path / "diagnostics", mode=options.mode_label),
        options=options,
        raw_report_dir=tmp_path / "diagnostics" / "raw",
        log=lambda _message: None,
        token="ghp_testtoken_value_1234567890",
    )


# ---------------------------------------------------------------------------
# Access preflight
# ---------------------------------------------------------------------------


def test_access_ok_when_all_read_scopes_available():
    client = FakeClient()
    client.add_repository("owner/repo", runs=build_runs(3))
    access = check_repository_access(client, "owner/repo")

    assert access.fatal_error is None
    assert access.can_manage_runs is True
    assert access.can_manage_branches is True
    statuses = {(check.scope, check.access): check.status for check in access.checks}
    assert statuses[("metadata", "read")] == "ok"
    assert statuses[("contents", "write")] == "ok"
    assert statuses[("pull-requests", "read")] == "ok"
    assert statuses[("actions", "read")] == "ok"
    assert statuses[("actions", "write")] == "unverified"


def test_access_missing_repository_is_fatal():
    client = FakeClient()
    access = check_repository_access(client, "owner/missing")

    assert access.fatal_error is not None
    assert "not found" in access.fatal_error
    assert access.can_manage_runs is False
    assert access.can_manage_branches is False


def test_read_only_token_cannot_manage_branches():
    client = FakeClient()
    client.add_repository("owner/repo", runs=build_runs(3), can_push=False)
    access = check_repository_access(client, "owner/repo")

    assert access.can_manage_branches is False
    assert any(check.scope == "contents" and check.status == "missing" for check in access.checks)


# ---------------------------------------------------------------------------
# Per-repository processing
# ---------------------------------------------------------------------------


def test_dry_run_reports_without_deleting(tmp_path):
    client = FakeClient()
    client.add_repository("owner/repo", runs=build_runs(75))
    cleanup = make_cleanup(client, tmp_path)

    report = cleanup.process("owner/repo")

    assert report.status == "ok"
    assert report.runs_examined == 75
    assert report.runs_retained == 20
    assert report.runs_deleted == 0
    assert report.runs_planned_deletions == 55
    assert client.delete_calls == []


def test_live_run_deletes_exactly_the_old_runs(tmp_path):
    client = FakeClient()
    client.add_repository("owner/repo", runs=build_runs(75))
    cleanup = make_cleanup(client, tmp_path, options=CleanupOptions(mode="live"))
    cleanup.writer.mode = "LIVE"

    report = cleanup.process("owner/repo")

    assert report.status == "ok"
    assert report.runs_deleted == 55
    assert len(client.runs["owner/repo"]) == 20
    assert client.delete_calls


def test_branch_results_are_merged_into_the_report(tmp_path):
    client = FakeClient()
    client.add_repository("owner/repo", runs=build_runs(5))
    runner = FakeBranchRunner(
        decisions={
            "owner/repo": [
                {"branch": "main", "action": "keep", "reason": "default branch"},
                {"branch": "merged", "action": "delete", "reason": "merged: ancestor"},
            ]
        }
    )
    cleanup = make_cleanup(client, tmp_path, branch_runner=runner)

    report = cleanup.process("owner/repo")

    assert report.branches_examined == 2
    assert report.branches_kept == 1
    assert report.branches_planned_deletions == 1
    assert report.branches_deleted == 0  # dry run
    assert {d.branch for d in report.branch_decisions} == {"main", "merged"}
    assert runner.calls[0]["live"] is False


def test_missing_contents_write_skips_branches_but_cleans_runs(tmp_path):
    client = FakeClient()
    client.add_repository("owner/repo", runs=build_runs(30), can_push=False)
    runner = FakeBranchRunner()
    cleanup = make_cleanup(client, tmp_path, branch_runner=runner)

    report = cleanup.process("owner/repo")

    assert report.runs_examined == 30
    assert runner.calls == []
    assert report.status == "failed"
    assert any("Contents: Read and write" in failure for failure in report.failures)


def test_missing_actions_read_skips_runs_but_cleans_branches(tmp_path):
    client = FakeClient()
    client.add_repository("owner/repo", runs=build_runs(30))
    client.forbid_runs.add("owner/repo")
    runner = FakeBranchRunner(decisions={"owner/repo": []})
    cleanup = make_cleanup(client, tmp_path, branch_runner=runner)

    report = cleanup.process("owner/repo")

    assert report.runs_examined == 0
    assert len(runner.calls) == 1
    assert report.status == "failed"
    assert any("Actions: Read" in failure for failure in report.failures)


def test_incomplete_run_listing_deletes_nothing(tmp_path):
    client = FakeClient()
    client.add_repository("owner/repo", runs=build_runs(120))
    client.fail_runs_page["owner/repo"] = 2
    cleanup = make_cleanup(client, tmp_path, options=CleanupOptions(mode="live"))

    report = cleanup.process("owner/repo")

    assert report.runs_deleted == 0
    assert client.delete_calls == []
    assert report.status == "failed"


def test_delete_permission_error_is_reported(tmp_path):
    client = FakeClient()
    client.add_repository("owner/repo", runs=build_runs(30))
    client.forbid_delete.add("owner/repo")
    cleanup = make_cleanup(client, tmp_path, options=CleanupOptions(mode="live"))

    report = cleanup.process("owner/repo")

    assert report.runs_deleted == 0
    assert report.runs_failed == 1
    assert any("Actions: Read and write" in failure for failure in report.failures)


def test_workspace_failure_is_recorded_and_runs_still_processed(tmp_path):
    client = FakeClient()
    client.add_repository("owner/repo", runs=build_runs(30))
    factory = FakeWorkspaceFactory(tmp_path / "ws", error=WorkspaceError("clone failed"))
    cleanup = make_cleanup(client, tmp_path, workspace_factory=factory)

    report = cleanup.process("owner/repo")

    assert report.runs_examined == 30
    assert report.status == "failed"
    assert any("isolated git workspace" in failure for failure in report.failures)


def test_branch_runner_failure_is_recorded(tmp_path):
    client = FakeClient()
    client.add_repository("owner/repo", runs=build_runs(5))
    runner = FakeBranchRunner(fail_for={"owner/repo"})
    cleanup = make_cleanup(client, tmp_path, branch_runner=runner)

    report = cleanup.process("owner/repo")

    assert report.status == "failed"
    assert any("branch cleanup exited with code 1" in failure for failure in report.failures)


def test_current_run_id_is_only_excluded_in_the_central_repository(tmp_path):
    client = FakeClient()
    client.add_repository("central/repo", runs=build_runs(25, first_id=5000))
    options = CleanupOptions(
        mode="live", keep_latest=20, central_repository="central/repo", current_run_id="5024"
    )
    cleanup = make_cleanup(client, tmp_path, options=options)

    report = cleanup.process("central/repo")

    assert 5024 not in {run_id for _repo, run_id in client.delete_calls}
    assert report.runs_deleted == 4  # 25 runs - current - 20 kept

    other = FakeClient()
    other.add_repository("other/repo", runs=build_runs(25, first_id=5000))
    cleanup_other = make_cleanup(other, tmp_path, options=options)
    cleanup_other.process("other/repo")
    # A run id that is not this repository's run is a normal candidate.
    assert len(other.delete_calls) == 5


def test_one_failure_does_not_stop_other_repositories(tmp_path):
    client = FakeClient()
    client.add_repository("owner/good", runs=build_runs(30))
    client.add_repository("owner/bad", runs=build_runs(30))
    client.fail_repos.add("owner/bad")
    client.add_repository("owner/also-good", runs=build_runs(10))
    cleanup = make_cleanup(client, tmp_path)

    reports = cleanup.run(["owner/good", "owner/bad", "owner/also-good"])
    statuses = {report.repository: report.status for report in reports}

    assert statuses["owner/good"] == "ok"
    assert statuses["owner/bad"] == "failed"
    assert statuses["owner/also-good"] == "ok"
    assert reports[0].runs_examined == 30
    assert reports[2].runs_examined == 10


def test_reports_are_written_per_repository_and_aggregated(tmp_path):
    client = FakeClient()
    client.add_repository("owner/a", runs=build_runs(75, first_id=1000))
    client.add_repository("owner/b", runs=build_runs(32, first_id=2000))
    client.add_repository("owner/c", runs=build_runs(8, first_id=3000))
    writer = ReportWriter(tmp_path / "diagnostics", mode="DRY RUN")
    cleanup = make_cleanup(client, tmp_path, writer=writer)

    reports = cleanup.run(["owner/a", "owner/b", "owner/c"])

    assert len(reports) == 3
    for repo in ("owner__a", "owner__b", "owner__c"):
        assert (tmp_path / "diagnostics" / "reports" / f"{repo}.json").exists()
        assert (tmp_path / "diagnostics" / "reports" / f"{repo}.md").exists()

    summary = json.loads((tmp_path / "diagnostics" / "summary.json").read_text(encoding="utf-8"))
    assert summary["mode"] == "DRY RUN"
    assert summary["totals"]["repositories"] == 3
    assert summary["totals"]["runs_examined"] == 115
    assert summary["totals"]["runs_retained"] == 48
    assert summary["totals"]["runs_planned_deletions"] == 67
    assert summary["totals"]["runs_deleted"] == 0

    per_repo = {
        item["repository"]: item["counts"]["runs_planned_deletions"] for item in summary["repositories"]
    }
    assert per_repo == {"owner/a": 55, "owner/b": 12, "owner/c": 0}


def test_unexpected_exception_is_isolated(tmp_path):
    client = FakeClient()
    client.add_repository("owner/boom", runs=build_runs(5))

    def exploding_get_repository(repo):
        raise RuntimeError("unexpected")

    client.get_repository = exploding_get_repository
    cleanup = make_cleanup(client, tmp_path)
    reports = cleanup.run(["owner/boom"])

    assert reports[0].status == "failed"
    assert "unexpected" in reports[0].failures[0]


# ---------------------------------------------------------------------------
# CLI safety gates
# ---------------------------------------------------------------------------


@pytest.fixture()
def config_root(tmp_path: Path) -> Path:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "repositories.txt").write_text(
        "# test list\nowner/a\nowner/b\n", encoding="utf-8"
    )
    # A realistic repository layout (the branch runner is replaced in these tests).
    (tmp_path / "tool").mkdir()
    (tmp_path / "tool" / "branch_cleanup.py").write_text("# stub\n", encoding="utf-8")
    return tmp_path


def cli_args(root: Path, *extra: str) -> list[str]:
    return [
        "--config",
        "config/repositories.txt",
        "--repo-root",
        str(root),
        "--reports-dir",
        str(root / "diagnostics"),
        *extra,
    ]


def test_cli_refuses_live_without_confirmation(config_root, monkeypatch):
    monkeypatch.setenv("CLEANUP_TOKEN", "token")
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)

    assert main(cli_args(config_root, "--mode", "live")) == EXIT_CONFIG
    assert main(cli_args(config_root, "--mode", "live", "--confirm-live", "yes")) == EXIT_CONFIG


def test_cli_refuses_live_on_schedule(config_root, monkeypatch):
    monkeypatch.setenv("CLEANUP_TOKEN", "token")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")

    assert main(cli_args(config_root, "--mode", "live", "--confirm-live", "DELETE")) == EXIT_CONFIG


def test_cli_requires_a_token(config_root, monkeypatch):
    monkeypatch.delenv("CLEANUP_TOKEN", raising=False)
    assert main(cli_args(config_root)) == EXIT_CONFIG


def test_cli_rejects_invalid_keep_latest(config_root, monkeypatch):
    monkeypatch.setenv("CLEANUP_TOKEN", "token")
    assert main(cli_args(config_root, "--keep-latest", "0")) == EXIT_CONFIG
    assert main(cli_args(config_root, "--keep-latest", "abc")) == EXIT_CONFIG


def test_cli_rejects_repository_outside_config(config_root, monkeypatch):
    monkeypatch.setenv("CLEANUP_TOKEN", "token")
    assert main(cli_args(config_root, "--repository", "evil/repo")) == EXIT_CONFIG


def test_cli_rejects_missing_config(config_root, monkeypatch):
    monkeypatch.setenv("CLEANUP_TOKEN", "token")
    args = [
        "--config",
        "config/missing.txt",
        "--repo-root",
        str(config_root),
        "--reports-dir",
        str(config_root / "diagnostics"),
    ]
    assert main(args) == EXIT_CONFIG


def test_cli_rejects_invalid_config_entries(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "repositories.txt").write_text("owner/a\nnot valid\n", encoding="utf-8")
    monkeypatch.setenv("CLEANUP_TOKEN", "token")

    assert main(cli_args(tmp_path)) == EXIT_CONFIG


def test_cli_dry_run_end_to_end(config_root, monkeypatch):
    monkeypatch.setenv("CLEANUP_TOKEN", "ghp_cli_test_token_1234567890")
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)

    client = FakeClient()
    client.add_repository("owner/a", runs=build_runs(25, first_id=1000))
    client.add_repository("owner/b", runs=build_runs(40, first_id=2000))
    runner = FakeBranchRunner(
        decisions={
            "owner/a": [{"branch": "main", "action": "keep", "reason": "default branch"}],
            "owner/b": [{"branch": "main", "action": "keep", "reason": "default branch"}],
        }
    )

    monkeypatch.setattr(central_cleanup, "GhCliClient", lambda **kwargs: client)
    monkeypatch.setattr(
        central_cleanup,
        "GitWorkspaceFactory",
        lambda **kwargs: FakeWorkspaceFactory(config_root / "ws"),
    )
    monkeypatch.setattr(central_cleanup, "BranchCleanupRunner", lambda **kwargs: runner)

    exit_code = main(cli_args(config_root))

    assert exit_code == EXIT_OK
    assert client.delete_calls == []
    assert all(call["live"] is False for call in runner.calls)
    summary = json.loads((config_root / "diagnostics" / "summary.json").read_text(encoding="utf-8"))
    assert summary["mode"] == "DRY RUN"
    assert summary["totals"]["repositories"] == 2
    assert summary["totals"]["runs_deleted"] == 0
    assert summary["totals"]["runs_planned_deletions"] == 25


def test_cli_returns_failure_when_a_repository_fails(config_root, monkeypatch):
    monkeypatch.setenv("CLEANUP_TOKEN", "ghp_cli_test_token_1234567890")

    client = FakeClient()
    client.add_repository("owner/a", runs=build_runs(5))
    client.add_repository("owner/b", runs=build_runs(5))
    client.fail_repos.add("owner/b")

    monkeypatch.setattr(central_cleanup, "GhCliClient", lambda **kwargs: client)
    monkeypatch.setattr(
        central_cleanup,
        "GitWorkspaceFactory",
        lambda **kwargs: FakeWorkspaceFactory(config_root / "ws"),
    )
    monkeypatch.setattr(central_cleanup, "BranchCleanupRunner", lambda **kwargs: FakeBranchRunner())

    assert main(cli_args(config_root)) == EXIT_FAILURES


def test_cli_live_runs_delete_only_when_confirmed(config_root, monkeypatch):
    monkeypatch.setenv("CLEANUP_TOKEN", "ghp_cli_test_token_1234567890")
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)

    client = FakeClient()
    client.add_repository("owner/a", runs=build_runs(25))
    client.add_repository("owner/b", runs=build_runs(30))

    monkeypatch.setattr(central_cleanup, "GhCliClient", lambda **kwargs: client)
    monkeypatch.setattr(
        central_cleanup,
        "GitWorkspaceFactory",
        lambda **kwargs: FakeWorkspaceFactory(config_root / "ws"),
    )
    monkeypatch.setattr(central_cleanup, "BranchCleanupRunner", lambda **kwargs: FakeBranchRunner())

    exit_code = main(cli_args(config_root, "--mode", "live", "--confirm-live", "DELETE"))

    assert exit_code == EXIT_OK
    assert len(client.delete_calls) == 5 + 10
    summary = json.loads((config_root / "diagnostics" / "summary.json").read_text(encoding="utf-8"))
    assert summary["mode"] == "LIVE"
    assert summary["totals"]["runs_deleted"] == 15


# ---------------------------------------------------------------------------
# Entry point / subprocess wiring (regressions found during the live dry run)
# ---------------------------------------------------------------------------


def test_direct_script_execution_works(repo_root: Path):
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "tool/central_cleanup.py", "--help"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    assert "central_cleanup" in proc.stdout


def test_module_execution_works(repo_root: Path):
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "tool.central_cleanup", "--help"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr


def test_branch_runner_uses_absolute_script_path(tmp_path: Path):
    runner = central_cleanup.BranchCleanupRunner(token="t", script_path=Path("tool/branch_cleanup.py"))
    assert runner.script_path.is_absolute()


def test_branch_runner_executes_in_the_isolated_directory(tmp_path: Path, token: str):
    """The child must run in the clone directory, not in the central repository."""
    script = tmp_path / "fake_branch_cleanup.py"
    script.write_text(
        """import argparse, json, os
parser = argparse.ArgumentParser()
parser.add_argument("--repo")
parser.add_argument("--skip-runs", action="store_true")
parser.add_argument("--delete", action="store_true")
parser.add_argument("--dry-run", action="store_true")
parser.add_argument("--json-report", default="")
args = parser.parse_args()
payload = {
    "repo": args.repo,
    "cwd": os.getcwd(),
    "live": bool(args.delete),
    "skip_runs": bool(args.skip_runs),
    "branches_examined": 0, "kept": 0, "deleted": 0,
    "planned_deletions": 0, "failed": 0, "results": [], "errors": [],
}
open(args.json_report, "w").write(json.dumps(payload))
""",
        encoding="utf-8",
    )
    workdir = tmp_path / "clone"
    workdir.mkdir()
    runner = central_cleanup.BranchCleanupRunner(token=token, script_path=script, log=lambda _m: None)
    result = runner.run(
        "owner/repo", live=True, workdir=workdir, report_path=tmp_path / "report.json"
    )

    assert result.ok is True, result.error
    assert result.report["cwd"] == str(workdir.resolve())
    assert result.report["live"] is True
    assert result.report["skip_runs"] is True


def test_branch_runner_missing_script_is_reported(tmp_path: Path, token: str):
    runner = central_cleanup.BranchCleanupRunner(
        token=token, script_path=tmp_path / "missing.py", log=lambda _m: None
    )
    result = runner.run("owner/repo", live=False, workdir=tmp_path, report_path=tmp_path / "r.json")

    assert result.ok is False
    assert "not found" in (result.error or "")


def test_cli_fails_when_the_branch_script_is_missing(tmp_path: Path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "repositories.txt").write_text("owner/a\n", encoding="utf-8")
    monkeypatch.setenv("CLEANUP_TOKEN", "token")

    assert main(cli_args(tmp_path)) == EXIT_CONFIG

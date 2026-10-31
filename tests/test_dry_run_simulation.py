"""Simulated dry run over mocked repositories (no network, no deletion).

This mirrors the acceptance example:

    repository A: 75 runs -> keep 20, delete 55
    repository B: 32 runs -> keep 20, delete 12
    repository C:  8 runs -> keep  8, delete  0
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.fakes import FakeBranchRunner, FakeClient, FakeWorkspaceFactory, build_runs
from tool import central_cleanup
from tool.central_cleanup import EXIT_OK, CleanupOptions, main
from tool.reports import ReportWriter

TOKEN = "ghp_simulationTOKENvalue1234567890abcdef"


def simulate(tmp_path: Path, *, mode: str = "dry-run"):
    client = FakeClient()
    client.authenticated_login = "acme"
    client.add_repository("acme/alpha", runs=build_runs(75, first_id=1000))
    client.add_repository("acme/beta", runs=build_runs(32, first_id=2000))
    client.add_repository("acme/gamma", runs=build_runs(8, first_id=3000))

    runner = FakeBranchRunner(
        decisions={
            "acme/alpha": [
                {"branch": "main", "action": "keep", "reason": "default branch"},
                {"branch": "release", "action": "keep", "reason": "protected branch"},
                {"branch": "feature-open", "action": "keep", "reason": "open pull request"},
                {"branch": "merged-old", "action": "delete", "reason": "merged: branch HEAD is ancestor of default branch"},
                {"branch": "wip", "action": "keep", "reason": "unmerged: branch has unique changes not in default"},
            ],
            "acme/beta": [
                {"branch": "main", "action": "keep", "reason": "default branch"},
                {"branch": "merged-pr", "action": "delete", "reason": "merged: PR merged and merge commit is ancestor of default (squash/rebase)"},
            ],
            "acme/gamma": [
                {"branch": "main", "action": "keep", "reason": "default branch"},
            ],
        }
    )

    options = CleanupOptions(mode=mode, keep_latest=20)
    writer = ReportWriter(tmp_path / "diagnostics", token=TOKEN, mode=options.mode_label)
    cleanup = central_cleanup.CentralCleanup(
        client=client,
        workspace_factory=FakeWorkspaceFactory(tmp_path / "ws"),
        branch_runner=runner,
        writer=writer,
        options=options,
        raw_report_dir=tmp_path / "diagnostics" / "raw",
        token=TOKEN,
        log=lambda _message: None,
    )
    reports = cleanup.run(["acme/alpha", "acme/beta", "acme/gamma"])
    return client, runner, reports, writer


def test_simulated_dry_run_keeps_twenty_per_repository(tmp_path: Path):
    client, runner, reports, writer = simulate(tmp_path)

    by_repo = {report.repository: report for report in reports}

    assert by_repo["acme/alpha"].runs_examined == 75
    assert by_repo["acme/alpha"].runs_retained == 20
    assert by_repo["acme/alpha"].runs_planned_deletions == 55
    assert by_repo["acme/beta"].runs_examined == 32
    assert by_repo["acme/beta"].runs_retained == 20
    assert by_repo["acme/beta"].runs_planned_deletions == 12
    assert by_repo["acme/gamma"].runs_examined == 8
    assert by_repo["acme/gamma"].runs_retained == 8
    assert by_repo["acme/gamma"].runs_planned_deletions == 0

    # Nothing was deleted and the branch runner always ran dry.
    assert client.delete_calls == []
    assert all(call["live"] is False for call in runner.calls)
    assert all(report.runs_deleted == 0 for report in reports)
    assert all(report.status == "DRY RUN" for report in reports)

    summary = json.loads((tmp_path / "diagnostics" / "summary.json").read_text(encoding="utf-8"))
    assert summary["mode"] == "DRY RUN"
    assert summary["totals"]["runs_examined"] == 115
    assert summary["totals"]["runs_retained"] == 48
    assert summary["totals"]["runs_planned_deletions"] == 67
    assert summary["totals"]["runs_deleted"] == 0
    assert summary["totals"]["branches_examined"] == 8
    assert summary["totals"]["branches_planned_deletions"] == 2


def test_simulated_report_contains_required_fields(tmp_path: Path):
    _client, _runner, _reports, writer = simulate(tmp_path)
    payload = json.loads((tmp_path / "diagnostics" / "summary.json").read_text(encoding="utf-8"))
    alpha = next(item for item in payload["repositories"] if item["repository"] == "acme/alpha")

    assert alpha["mode"] == "DRY RUN"
    assert alpha["started_at"] and alpha["finished_at"]
    assert alpha["keep_latest"] == 20
    assert alpha["default_branch"] == "main"
    assert alpha["counts"]["runs_examined"] == 75
    assert alpha["counts"]["runs_retained"] == 20
    assert alpha["counts"]["runs_deleted"] == 0
    assert alpha["counts"]["runs_planned_deletions"] == 55
    assert alpha["counts"]["runs_excluded"] == 0
    assert alpha["counts"]["branches_examined"] == 5
    assert alpha["counts"]["branches_kept"] == 4
    assert alpha["counts"]["branches_planned_deletions"] == 1
    assert alpha["failure_count"] == 0

    reasons = {decision["branch"]: decision["reason"] for decision in alpha["branch_decisions"]}
    assert reasons["main"] == "default branch"
    assert reasons["release"] == "protected branch"
    assert reasons["feature-open"] == "open pull request"
    assert reasons["merged-old"].startswith("merged")

    report_md = (tmp_path / "diagnostics" / "REPORT.md").read_text(encoding="utf-8")
    assert "`acme/alpha`" in report_md
    assert "Branch decisions" in (tmp_path / "diagnostics" / "reports" / "acme__alpha.md").read_text(
        encoding="utf-8"
    )


def test_simulation_never_leaks_the_token(tmp_path: Path):
    simulate(tmp_path)
    for path in list((tmp_path / "diagnostics").rglob("*")):
        if path.is_file():
            assert TOKEN not in path.read_text(encoding="utf-8", errors="ignore"), path


def test_api_failure_in_simulation_blocks_deletion(tmp_path: Path):
    client = FakeClient()
    client.authenticated_login = "acme"
    client.add_repository("acme/alpha", runs=build_runs(75))
    client.add_repository("acme/beta", runs=build_runs(32))
    client.fail_runs_page["acme/alpha"] = 1

    options = CleanupOptions(mode="live", keep_latest=20)
    writer = ReportWriter(tmp_path / "diagnostics", mode="LIVE")
    cleanup = central_cleanup.CentralCleanup(
        client=client,
        workspace_factory=FakeWorkspaceFactory(tmp_path / "ws"),
        branch_runner=FakeBranchRunner(),
        writer=writer,
        options=options,
        raw_report_dir=tmp_path / "diagnostics" / "raw",
        log=lambda _message: None,
    )
    reports = cleanup.run(["acme/alpha", "acme/beta"])

    alpha, beta = reports
    assert alpha.runs_deleted == 0
    assert alpha.status == "FAILED"
    assert beta.runs_deleted == 12  # the healthy repository is still processed
    assert {repo for repo, _ in client.delete_calls} == {"acme/beta"}


def test_cli_simulation_writes_reports(tmp_path: Path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "repositories.txt").write_text(
        "acme/alpha\nacme/beta\nacme/gamma\n", encoding="utf-8"
    )
    (tmp_path / "tool").mkdir()
    (tmp_path / "tool" / "branch_cleanup.py").write_text("# stub\n", encoding="utf-8")
    monkeypatch.setenv("CLEANUP_TOKEN", TOKEN)
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    client = FakeClient()
    client.authenticated_login = "acme"
    client.add_repository("acme/alpha", runs=build_runs(75))
    client.add_repository("acme/beta", runs=build_runs(32))
    client.add_repository("acme/gamma", runs=build_runs(8))

    monkeypatch.setattr(central_cleanup, "GhCliClient", lambda **kwargs: client)
    monkeypatch.setattr(
        central_cleanup,
        "GitWorkspaceFactory",
        lambda **kwargs: FakeWorkspaceFactory(tmp_path / "ws"),
    )
    monkeypatch.setattr(central_cleanup, "BranchCleanupRunner", lambda **kwargs: FakeBranchRunner())

    exit_code = main(
        [
            "--config",
            "config/repositories.txt",
            "--repo-root",
            str(tmp_path),
            "--reports-dir",
            str(tmp_path / "diagnostics"),
            "--mode",
            "dry-run",
            "--keep-latest",
            "20",
        ]
    )

    assert exit_code == EXIT_OK
    assert client.delete_calls == []
    summary = json.loads((tmp_path / "diagnostics" / "summary.json").read_text(encoding="utf-8"))
    assert [item["repository"] for item in summary["repositories"]] == [
        "acme/alpha",
        "acme/beta",
        "acme/gamma",
    ]
    assert summary["totals"]["runs_planned_deletions"] == 67

"""Token handling: environment only, never logged, never persisted."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tool.central_cleanup import BranchCleanupRunner
from tool.git_workspace import CREDENTIAL_HELPER, credential_helper_uses_env_only
from tool.github_client import (
    ApiResult,
    CleanupTokenError,
    GhCliClient,
    build_child_env,
    redact,
    resolve_token,
)
from tool.reports import BranchDecision, RepoReport, ReportWriter


def test_resolve_token_reads_environment():
    assert resolve_token({"CLEANUP_TOKEN": "abc123"}, env_var="CLEANUP_TOKEN") == "abc123"


def test_resolve_token_missing_raises_without_echoing_anything():
    with pytest.raises(CleanupTokenError) as excinfo:
        resolve_token({}, env_var="CLEANUP_TOKEN")
    message = str(excinfo.value)
    assert "CLEANUP_TOKEN" in message
    assert "abc" not in message


def test_resolve_token_rejects_whitespace():
    with pytest.raises(CleanupTokenError):
        resolve_token({"CLEANUP_TOKEN": "abc def"}, env_var="CLEANUP_TOKEN")


def test_child_env_replaces_central_tokens(token: str):
    base = {
        "PATH": "/usr/bin",
        "GITHUB_TOKEN": "central-token",
        "GH_TOKEN": "central-token",
        "GITHUB_ENTERPRISE_TOKEN": "central-token",
    }
    env = build_child_env(token, base=base)

    assert env["GH_TOKEN"] == token
    assert env["CLEANUP_TOKEN"] == token
    assert env["PATH"] == "/usr/bin"
    assert "GITHUB_TOKEN" not in env
    assert "GITHUB_ENTERPRISE_TOKEN" not in env
    assert "central-token" not in env.values()


def test_child_env_cannot_fall_back_to_the_central_token(token: str):
    env = build_child_env(token, base={"GITHUB_TOKEN": "central-token", "GH_TOKEN": "central-token"})
    assert env["GH_TOKEN"] == token


def test_redact_removes_exact_token(token: str):
    assert token not in redact(f"authorization: {token}", token)
    assert "***REDACTED***" in redact(f"authorization: {token}", token)


def test_redact_removes_github_token_shapes():
    text = "failed with ghp_abcdefghijklmnopqrstuvwxyz0123 and github_pat_11ABCDEFG0123456789"
    cleaned = redact(text)
    assert "ghp_" not in cleaned
    assert "github_pat_" not in cleaned
    assert cleaned.count("***REDACTED***") == 2


def test_client_errors_are_redacted(token: str):
    def runner(cmd, env, timeout):
        return subprocess.CompletedProcess(cmd, 1, "", f"gh: bad token {token} (HTTP 500)")

    client = GhCliClient(token=token, runner=runner, sleep=lambda _s: None, log=lambda _m: None)
    result = client.api("repos/o/r")

    assert result.ok is False
    assert token not in (result.error or "")
    assert "***REDACTED***" in (result.error or "")


def test_client_retries_rate_limits_then_succeeds(token: str):
    calls = {"n": 0}

    def runner(cmd, env, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            return subprocess.CompletedProcess(cmd, 1, "", "gh: API rate limit exceeded (HTTP 403)")
        return subprocess.CompletedProcess(cmd, 0, '{"id": 1}', "")

    client = GhCliClient(token=token, runner=runner, sleep=lambda _s: None, log=lambda _m: None)
    result = client.api("repos/o/r")

    assert result.ok is True
    assert calls["n"] == 2


def test_delete_treats_404_as_success(token: str):
    def runner(cmd, env, timeout):
        return subprocess.CompletedProcess(cmd, 1, "", "gh: Not Found (HTTP 404)")

    client = GhCliClient(token=token, runner=runner, sleep=lambda _s: None, log=lambda _m: None)
    result = client.delete_workflow_run("o/r", 5)

    assert result.ok is True


def test_branch_runner_environment_is_sanitised(token: str, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "central-token")
    monkeypatch.setenv("GH_TOKEN", "central-token")
    monkeypatch.setenv("DELETE_BRANCHES", "true")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", "/tmp/summary")

    runner = BranchCleanupRunner(token=token, script_path=Path("tool/branch_cleanup.py"))
    env = runner._child_env()

    assert env["GH_TOKEN"] == token
    assert env["CLEANUP_TOKEN"] == token
    assert "GITHUB_TOKEN" not in env
    assert "DELETE_BRANCHES" not in env
    assert "GITHUB_STEP_SUMMARY" not in env
    assert "central-token" not in env.values()


def test_credential_helper_uses_environment_only():
    assert credential_helper_uses_env_only(CREDENTIAL_HELPER)
    assert "$CLEANUP_TOKEN" in CREDENTIAL_HELPER
    assert "ghp_" not in CREDENTIAL_HELPER


def test_reports_never_contain_the_token(token: str, tmp_path: Path):
    report = RepoReport(repository="owner/repo", mode="DRY RUN", keep_latest=20)
    report.add_note(f"accidental leak {token}")
    report.branch_decisions.append(BranchDecision("feature", "keep", token))

    writer = ReportWriter(tmp_path, token=token, mode="DRY RUN")
    paths = writer.write_repository(report)
    aggregate = writer.write_aggregate([report])

    for path in (paths["json"], paths["markdown"], aggregate["json"], aggregate["markdown"]):
        content = path.read_text(encoding="utf-8")
        assert token not in content
        assert "REDACTED" in content


def test_report_writer_writes_expected_counts(tmp_path: Path):
    report = RepoReport(
        repository="owner/repo",
        mode="LIVE",
        runs_examined=75,
        runs_retained=20,
        runs_deleted=55,
        branches_examined=4,
        branches_kept=3,
        branches_deleted=1,
    )
    report.finished_at = "2026-01-01T00:00:00Z"
    writer = ReportWriter(tmp_path, mode="LIVE")
    paths = writer.write_repository(report)
    import json

    payload = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert payload["mode"] == "LIVE"
    assert payload["counts"]["runs_deleted"] == 55
    assert payload["counts"]["branches_deleted"] == 1
    assert payload["finished_at"] == "2026-01-01T00:00:00Z"


def test_repository_name_cannot_inject_git_options(tmp_path: Path, token: str):
    from tool.git_workspace import GitWorkspaceFactory, WorkspaceError

    factory = GitWorkspaceFactory(token=token, base_dir=tmp_path)
    with pytest.raises(WorkspaceError):
        with factory.create("--upload-pack=evil/repo"):
            pass  # pragma: no cover


def test_api_result_helpers():
    assert ApiResult(False, 403).forbidden
    assert ApiResult(False, 404).not_found
    assert not ApiResult(True, 200).forbidden

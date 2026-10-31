"""Contract tests for the central workflow definition and repository layout."""

from __future__ import annotations

import py_compile
from pathlib import Path

import yaml

CLEANUP_WORKFLOW = Path(".github/workflows/cleanup-old-runs.yml")
CI_WORKFLOW = Path(".github/workflows/ci.yml")


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_scheduled_and_manual_triggers_exist(repo_root: Path):
    document = load(repo_root / CLEANUP_WORKFLOW)
    triggers = document.get("on") or document.get(True)
    assert "schedule" in triggers
    assert triggers["schedule"][0]["cron"] == "0 2 * * *"
    assert "workflow_dispatch" in triggers

    inputs = triggers["workflow_dispatch"]["inputs"]
    assert inputs["mode"]["default"] == "dry-run"
    assert inputs["mode"]["options"] == ["dry-run", "live"]
    assert inputs["keep_latest"]["default"] == "20"
    assert inputs["repository"]["default"] == "all"
    assert inputs["confirm_live"]["default"] == ""


def test_schedule_is_always_a_dry_run(repo_root: Path):
    text = (repo_root / CLEANUP_WORKFLOW).read_text(encoding="utf-8")
    assert 'EVENT_NAME}" == "schedule"' in text
    assert "mode=dry-run" in text
    # The live branch must require the explicit confirmation word.
    assert '!= "DELETE"' in text
    assert 'echo "mode=${mode}"' in text


def test_workflow_token_is_read_only(repo_root: Path):
    document = load(repo_root / CLEANUP_WORKFLOW)
    assert document["permissions"] == {"contents": "read"}
    assert document["jobs"]["cleanup"]["permissions"] == {"contents": "read"}

    text = (repo_root / CLEANUP_WORKFLOW).read_text(encoding="utf-8")
    assert "github.token" not in text
    assert "secrets.GITHUB_TOKEN" not in text


def test_workflow_uses_the_configured_secret(repo_root: Path):
    text = (repo_root / CLEANUP_WORKFLOW).read_text(encoding="utf-8")
    assert "CLEANUP_TOKEN: ${{ secrets.CLEANUP_TOKEN }}" in text
    assert "GH_TOKEN" not in text  # children get theirs from the central script


def test_workflow_runs_the_central_orchestrator(repo_root: Path):
    text = (repo_root / CLEANUP_WORKFLOW).read_text(encoding="utf-8")
    assert "python3 tool/central_cleanup.py" in text
    assert "--central-repository" in text
    assert "--current-run-id" in text
    # Time-based retention was replaced by the newest-N policy.
    assert "cutoff" not in text.lower()
    # The branch algorithm is invoked by the orchestrator, not directly.
    assert "branch_cleanup.py" not in text


def test_cleanup_job_is_guarded_to_the_default_branch(repo_root: Path):
    document = load(repo_root / CLEANUP_WORKFLOW)
    job = document["jobs"]["cleanup"]
    assert "default_branch" in job["if"]


def test_reports_are_uploaded_even_on_failure(repo_root: Path):
    document = load(repo_root / CLEANUP_WORKFLOW)
    upload = [
        step
        for step in document["jobs"]["cleanup"]["steps"]
        if str(step.get("uses", "")).startswith("actions/upload-artifact")
    ]
    assert upload, "reports must be uploaded"
    assert upload[0]["if"] == "always()"
    assert "diagnostics/" in upload[0]["with"]["path"]


def test_checkout_does_not_persist_credentials(repo_root: Path):
    document = load(repo_root / CLEANUP_WORKFLOW)
    checkout = [
        step
        for step in document["jobs"]["cleanup"]["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout")
    ]
    assert checkout
    assert checkout[0]["with"]["persist-credentials"] is False


def test_ci_workflow_never_receives_the_cleanup_token(repo_root: Path):
    text = (repo_root / CI_WORKFLOW).read_text(encoding="utf-8")
    assert "CLEANUP_TOKEN" not in text
    document = load(repo_root / CI_WORKFLOW)
    assert document["permissions"] == {"contents": "read"}
    assert "pytest" in text
    assert "check_workflow_permissions.py" in text


def test_configuration_file_is_present_and_parsable(repo_root: Path):
    from tool.repositories import load_repository_config

    parsed = load_repository_config("config/repositories.txt", repo_root=repo_root)
    assert parsed.errors == ()
    assert parsed.repositories


def test_all_python_files_compile(repo_root: Path):
    files = sorted((repo_root / "tool").glob("*.py")) + sorted((repo_root / "tests").glob("*.py"))
    assert files
    for path in files:
        py_compile.compile(str(path), doraise=True, cfile=str(repo_root / ".pycache-check"))


def test_all_workflow_files_are_valid_yaml(repo_root: Path):
    files = sorted((repo_root / ".github" / "workflows").glob("*.y*ml"))
    assert files
    for path in files:
        assert isinstance(load(path), dict)

"""Execute the real workflow Bash steps with a fake python3/CLI transport.

These tests close the gap between YAML string assertions and Python unit
tests: workflow inputs -> Resolve run mode -> Run central cleanup -> argparse
-> retention -> DELETE command -> report/exit code.  No network is used.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github/workflows/cleanup-old-runs.yml"
TARGET = "kinanmjeed88/Scan-id"
CENTRAL = "kinanmjeed88/cleanup-old-runs.yml-"
FAKE_TOKEN = "ghp_WORKFLOW_TEST_TOKEN_NOT_REAL_1234567890"
EXPRESSION = re.compile(r"\$\{\{\s*([^}]+?)\s*\}\}")


def _document() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _step(name: str) -> dict:
    return next(step for step in _document()["jobs"]["cleanup"]["steps"] if step.get("name") == name)


def _render(text: str, context: dict) -> str:
    def lookup(match: re.Match) -> str:
        value = context
        for part in match.group(1).strip().split("."):
            value = value[part]
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    return EXPRESSION.sub(lookup, text)


def _execute_step(name: str, tmp_path: Path, env: dict[str, str], context: dict) -> subprocess.CompletedProcess:
    step = _step(name)
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    script = tmp_path / slug / "step.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(_render(step["run"], context), encoding="utf-8")
    return subprocess.run(
        ["bash", str(script)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _setup(tmp_path: Path, event: str, inputs: dict) -> tuple[dict[str, str], dict, Path, Path]:
    document = _document()
    triggers = document.get("on") or document.get(True)
    defaults = {name: spec.get("default", "") for name, spec in triggers["workflow_dispatch"]["inputs"].items()}
    defaults.update(inputs)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    shim = fake_bin / "python3"
    shim.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "from tests.workflow_fakes import main\n"
        "sys.exit(main())\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)

    record_path = tmp_path / "fake-record.json"
    summary_path = tmp_path / "step-summary.md"
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"GH_TOKEN", "GITHUB_TOKEN", "CLEANUP_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"}
    }
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env.get('PATH', os.defpath)}",
            "PYTHONPATH": str(REPO_ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
            "CLEANUP_TOKEN": FAKE_TOKEN,
            "GITHUB_EVENT_NAME": event,
            "GITHUB_REPOSITORY": CENTRAL,
            "GITHUB_RUN_ID": "999999",
            "GITHUB_OUTPUT": str(tmp_path / "github-output.txt"),
            "GITHUB_STEP_SUMMARY": str(summary_path),
            "FAKE_RECORD_PATH": str(record_path),
            "FAKE_REPORTS_DIR": str(tmp_path / "diagnostics"),
        }
    )
    context = {
        "github": {
            "event_name": event,
            "repository": CENTRAL,
            "run_id": "999999",
        },
        "inputs": defaults,
        "secrets": {"CLEANUP_TOKEN": FAKE_TOKEN},
        "steps": {"mode": {"outputs": {}}},
    }
    return env, context, record_path, summary_path


def _resolve_mode(tmp_path: Path, env: dict[str, str], context: dict) -> subprocess.CompletedProcess:
    step = _step("Resolve run mode")
    for key, expression in step["env"].items():
        env[key] = _render(expression, context)
    return _execute_step("Resolve run mode", tmp_path, env, context)


def _run_cleanup(
    tmp_path: Path,
    env: dict[str, str],
    context: dict,
    *,
    omit_keep_env: bool = False,
) -> subprocess.CompletedProcess:
    outputs = {}
    output_file = Path(env["GITHUB_OUTPUT"])
    if output_file.exists():
        for line in output_file.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            outputs[key] = value
    context["steps"]["mode"]["outputs"] = outputs
    step = _step("Run central cleanup")
    for key, expression in step["env"].items():
        env[key] = _render(expression, context)
    if omit_keep_env:
        env.pop("KEEP_LATEST", None)
    return _execute_step("Run central cleanup", tmp_path, env, context)


def _record(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _repo_summary(record: dict, repository: str = TARGET) -> dict:
    return next(repo for repo in record["summary"]["repositories"] if repo["repository"] == repository)


def test_manual_live_reaches_real_delete_executor_with_fake_api(tmp_path):
    env, context, record_path, summary_path = _setup(
        tmp_path,
        "workflow_dispatch",
        {"mode": "live", "confirm_live": "DELETE", "repository": TARGET},
    )
    mode = _resolve_mode(tmp_path, env, context)
    assert mode.returncode == 0, mode.stderr
    assert "mode=live" in Path(env["GITHUB_OUTPUT"]).read_text(encoding="utf-8")

    run = _run_cleanup(tmp_path, env, context)
    assert run.returncode == 0, run.stdout + run.stderr
    record = _record(record_path)
    args = record["argv"]
    assert args[args.index("--mode") + 1] == "live"
    assert args[args.index("--confirm-live") + 1] == "DELETE"
    assert args[args.index("--keep-latest") + 1] == "50"
    assert args[args.index("--repository") + 1] == TARGET

    repo = _repo_summary(record)
    assert record["summary"]["mode"] == "LIVE"
    assert repo["status"] == "LIVE_SUCCESS"
    assert repo["counts"]["runs_examined"] == 739
    assert repo["counts"]["runs_retained"] == 50
    assert repo["counts"]["runs_planned_deletions"] == 689
    assert repo["counts"]["runs_deleted"] == 689
    assert repo["counts"]["runs_failed"] == 0
    assert repo["retention"]["listing_complete"] is True
    assert len(record["delete_calls"]) == 689
    assert record["remaining_runs"][TARGET] == 50
    assert record["remaining_runs"][CENTRAL] == 60  # selector really limits the target
    assert all(repo_name == TARGET for repo_name, _ in record["delete_calls"])
    assert all(call["method"] == "GET" or call["endpoint"].startswith(f"repos/{TARGET}/actions/runs/") for call in record["api_calls"])
    assert "LIVE" in summary_path.read_text(encoding="utf-8")
    assert FAKE_TOKEN not in json.dumps(record)
    assert FAKE_TOKEN not in run.stdout + run.stderr


def test_manual_live_runs_only_skips_branch_phase(tmp_path):
    env, context, record_path, _ = _setup(
        tmp_path,
        "workflow_dispatch",
        {"mode": "live", "confirm_live": "DELETE", "repository": TARGET, "skip_branches": True},
    )
    assert _resolve_mode(tmp_path, env, context).returncode == 0
    run = _run_cleanup(tmp_path, env, context)
    assert run.returncode == 0, run.stdout + run.stderr
    record = _record(record_path)
    assert "--skip-branches" in record["argv"]
    assert record["branch_calls"] == []
    assert _repo_summary(record)["counts"]["runs_deleted"] == 689
    assert _repo_summary(record)["counts"]["branches_examined"] == 0
    assert record["remaining_runs"][TARGET] == 50


def test_manual_live_first_delete_403_is_visible_and_stops_repository(tmp_path):
    env, context, record_path, summary_path = _setup(
        tmp_path,
        "workflow_dispatch",
        {"mode": "live", "confirm_live": "DELETE", "repository": TARGET},
    )
    env["FAKE_FIRST_DELETE_FORBIDDEN"] = "true"
    assert _resolve_mode(tmp_path, env, context).returncode == 0
    run = _run_cleanup(tmp_path, env, context)

    assert run.returncode == 1
    record = _record(record_path)
    repo = _repo_summary(record)
    assert record["summary"]["mode"] == "LIVE"
    assert repo["status"] == "FAILED"
    assert repo["counts"]["runs_deleted"] == 0
    assert repo["counts"]["runs_failed"] == 1
    assert len(record["delete_calls"]) == 1
    assert record["remaining_runs"][TARGET] == 739
    assert "403" in repo["retention"]["stop_reason"]
    assert "Actions: Read and write" in repo["retention"]["stop_reason"]
    assert any("403" in failure for failure in repo["failures"])
    assert "::error::" in run.stdout + run.stderr
    summary = summary_path.read_text(encoding="utf-8")
    assert "FAILED" in summary
    assert "403" in summary


@pytest.mark.parametrize("event", ["schedule", "workflow_dispatch"])
def test_default_and_scheduled_paths_never_delete(tmp_path, event):
    inputs = {"repository": TARGET}
    if event == "schedule":
        # Even hostile/irrelevant input values cannot override the trigger.
        inputs.update({"mode": "live", "confirm_live": "DELETE"})
    env, context, record_path, _ = _setup(tmp_path, event, inputs)
    assert _resolve_mode(tmp_path, env, context).returncode == 0
    run = _run_cleanup(tmp_path, env, context)

    assert run.returncode == 0, run.stdout + run.stderr
    record = _record(record_path)
    args = record["argv"]
    assert args[args.index("--mode") + 1] == "dry-run"
    assert "--confirm-live" not in args
    assert record["summary"]["mode"] == "DRY RUN"
    assert _repo_summary(record)["counts"]["runs_planned_deletions"] == 689
    assert record["delete_calls"] == []
    assert record["remaining_runs"][TARGET] == 739
    assert all(call["live"] is False for call in record["branch_calls"])
    assert not any(call["method"] == "DELETE" for call in record["api_calls"])


@pytest.mark.parametrize("confirm", ["", "delete", "yes"])
def test_manual_live_without_exact_confirmation_stops_before_python(tmp_path, confirm):
    env, context, record_path, _ = _setup(
        tmp_path, "workflow_dispatch", {"mode": "live", "confirm_live": confirm}
    )
    result = _resolve_mode(tmp_path, env, context)
    assert result.returncode == 1
    assert "confirm_live=DELETE" in result.stdout + result.stderr
    assert not record_path.exists()


def test_invalid_manual_mode_is_rejected_not_silently_downgraded(tmp_path):
    env, context, record_path, _ = _setup(tmp_path, "workflow_dispatch", {"mode": "LIVE"})
    result = _resolve_mode(tmp_path, env, context)
    assert result.returncode == 1
    assert "invalid mode" in result.stdout + result.stderr
    assert not record_path.exists()


def test_unsupported_event_cannot_reach_cleanup(tmp_path):
    env, context, record_path, _ = _setup(tmp_path, "push", {"mode": "live", "confirm_live": "DELETE"})
    result = _resolve_mode(tmp_path, env, context)
    assert result.returncode == 1
    assert "refusing event" in result.stdout + result.stderr
    assert not record_path.exists()


def test_missing_keep_env_uses_fifty_not_the_old_twenty(tmp_path):
    env, context, record_path, _ = _setup(
        tmp_path, "workflow_dispatch", {"mode": "dry-run", "repository": TARGET}
    )
    assert _resolve_mode(tmp_path, env, context).returncode == 0
    run = _run_cleanup(tmp_path, env, context, omit_keep_env=True)
    assert run.returncode == 0, run.stdout + run.stderr
    record = _record(record_path)
    assert record["argv"][record["argv"].index("--keep-latest") + 1] == "50"
    assert _repo_summary(record)["counts"]["runs_retained"] == 50
    assert record["delete_calls"] == []


def test_workflow_and_cli_defaults_are_both_fifty():
    document = _document()
    triggers = document.get("on") or document.get(True)
    assert triggers["workflow_dispatch"]["inputs"]["keep_latest"]["default"] == "50"
    assert "${KEEP_LATEST:-50}" in _step("Run central cleanup")["run"]
    assert ":-20}" not in _step("Run central cleanup")["run"]

    from tool.central_cleanup import CleanupOptions, build_parser
    from tool.run_retention import DEFAULT_KEEP_LATEST

    assert DEFAULT_KEEP_LATEST == 50
    assert CleanupOptions().keep_latest == 50
    assert build_parser().parse_args([]).keep_latest == "50"


@pytest.mark.parametrize(
    ("completed", "expected_deleted", "expected_remaining"),
    [(20, 0, 20), (50, 0, 50), (51, 1, 50), (100, 50, 50), (750, 700, 50), (1000, 950, 50)],
)
def test_manual_live_retention_matrix(tmp_path, completed, expected_deleted, expected_remaining):
    env, context, record_path, _ = _setup(
        tmp_path,
        "workflow_dispatch",
        {"mode": "live", "confirm_live": "DELETE", "repository": TARGET, "skip_branches": True},
    )
    env["FAKE_RUN_COUNT"] = str(completed)
    assert _resolve_mode(tmp_path, env, context).returncode == 0
    run = _run_cleanup(tmp_path, env, context)
    assert run.returncode == 0, run.stdout + run.stderr
    record = _record(record_path)
    repo = _repo_summary(record)
    assert repo["status"] == "LIVE_SUCCESS"
    assert repo["counts"]["runs_examined"] == completed
    assert repo["counts"]["runs_completed"] == completed
    assert repo["counts"]["runs_deleted"] == expected_deleted
    assert repo["counts"]["runs_remaining"] == expected_remaining
    assert record["remaining_runs"][TARGET] == expected_remaining
    assert len(record["delete_calls"]) == expected_deleted
    assert repo["verification"]["status"] == ("verified" if expected_deleted else "not_attempted")


def test_manual_live_permission_loss_after_partial_success_is_partial_failure(tmp_path):
    env, context, record_path, summary_path = _setup(
        tmp_path,
        "workflow_dispatch",
        {"mode": "live", "confirm_live": "DELETE", "repository": TARGET, "skip_branches": True},
    )
    env["FAKE_RUN_COUNT"] = "60"
    env["FAKE_FORBID_DELETE_AFTER"] = "3"
    assert _resolve_mode(tmp_path, env, context).returncode == 0
    run = _run_cleanup(tmp_path, env, context)
    assert run.returncode == 1
    record = _record(record_path)
    repo = _repo_summary(record)
    assert repo["status"] == "PARTIAL_FAILURE"
    assert repo["counts"]["runs_deleted"] == 3
    assert repo["counts"]["runs_failed"] == 1
    assert repo["counts"]["runs_remaining"] == 57
    assert len(record["delete_calls"]) == 4
    assert "403" in repo["retention"]["stop_reason"]
    assert repo["verification"]["expected_remaining"] == 57
    assert repo["verification"]["failed_ids"] == [100003]
    assert repo["verification"]["unattempted_ids"] == list(range(100004, 100010))
    assert "PARTIAL_FAILURE" in summary_path.read_text(encoding="utf-8")


def test_workflow_central_repository_with_18_runs_retains_all_and_excludes_only_branches(tmp_path):
    env, context, record_path, summary_path = _setup(
        tmp_path,
        "workflow_dispatch",
        {"mode": "live", "confirm_live": "DELETE", "repository": CENTRAL, "skip_branches": False},
    )
    env["FAKE_CENTRAL_RUN_COUNT"] = "18"
    assert _resolve_mode(tmp_path, env, context).returncode == 0
    run = _run_cleanup(tmp_path, env, context)

    assert run.returncode == 0, run.stdout + run.stderr
    record = _record(record_path)
    repo = _repo_summary(record, CENTRAL)
    assert repo["status"] == "LIVE_SUCCESS"
    assert repo["counts"]["runs_examined"] == 18
    assert repo["counts"]["runs_completed"] == 18
    assert repo["counts"]["runs_retained"] == 18
    assert repo["counts"]["runs_planned_deletions"] == 0
    assert repo["counts"]["runs_deleted"] == 0
    assert repo["counts"]["runs_remaining"] == 18
    assert repo["counts"]["branches_examined"] == 0
    assert record["branch_calls"] == []
    assert record["delete_calls"] == []
    assert record["remaining_runs"][CENTRAL] == 18
    assert any("branch cleanup excluded for the central repository" in note for note in repo["notes"])
    assert any(
        call["method"] == "GET" and call["endpoint"].startswith(f"repos/{CENTRAL}/actions/runs?")
        for call in record["api_calls"]
    )
    reports_dir = Path(env["FAKE_REPORTS_DIR"])
    markdown = (reports_dir / "reports" / f"{CENTRAL.replace('/', '__')}.md").read_text(encoding="utf-8")
    assert "- Retained (newest 50 completed): 18" in markdown
    assert "- Deleted: 0" in markdown
    assert "- Remaining: 18" in markdown
    assert "| 18 | 18 | 0 | 18 | not_attempted | 0 |" in summary_path.read_text(encoding="utf-8")


def test_workflow_422_is_recorded_as_failure_and_processing_continues(tmp_path):
    env, context, record_path, summary_path = _setup(
        tmp_path,
        "workflow_dispatch",
        {"mode": "live", "confirm_live": "DELETE", "repository": TARGET, "skip_branches": True},
    )
    env["FAKE_RUN_COUNT"] = "53"
    env["FAKE_DELETE_FAILURES"] = json.dumps({"100000": 422})
    assert _resolve_mode(tmp_path, env, context).returncode == 0
    run = _run_cleanup(tmp_path, env, context)

    assert run.returncode == 1
    record = _record(record_path)
    repo = _repo_summary(record)
    assert repo["status"] == "PARTIAL_FAILURE"
    assert repo["counts"]["runs_deleted"] == 2
    assert repo["counts"]["runs_failed"] == 1
    assert repo["counts"]["runs_already_absent"] == 0
    assert repo["counts"]["runs_remaining"] == 51
    assert [run_id for _repo, run_id in record["delete_calls"]] == [100000, 100001, 100002]
    assert sum(call["method"] == "DELETE" for call in record["api_calls"]) == 3  # no retry of 422
    assert repo["verification"]["expected_remaining"] == 51
    assert repo["verification"]["failed_ids"] == [100000]
    assert repo["verification"]["still_present_failed_ids"] == [100000]
    assert any("422" in failure for failure in repo["failures"])
    reports_dir = Path(env["FAKE_REPORTS_DIR"])
    markdown = (reports_dir / "reports" / f"{TARGET.replace('/', '__')}.md").read_text(encoding="utf-8")
    assert "- Failed: 1" in markdown
    assert "422" in markdown
    summary = summary_path.read_text(encoding="utf-8")
    assert "PARTIAL_FAILURE" in summary
    assert "422" in summary


def test_workflow_delete_404_is_already_absent_not_deleted_and_is_verified(tmp_path):
    env, context, record_path, _ = _setup(
        tmp_path,
        "workflow_dispatch",
        {"mode": "live", "confirm_live": "DELETE", "repository": TARGET, "skip_branches": True},
    )
    env["FAKE_RUN_COUNT"] = "53"
    env["FAKE_ABSENT_ON_DELETE"] = json.dumps([100000])
    assert _resolve_mode(tmp_path, env, context).returncode == 0
    run = _run_cleanup(tmp_path, env, context)

    assert run.returncode == 0, run.stdout + run.stderr
    record = _record(record_path)
    repo = _repo_summary(record)
    assert repo["status"] == "LIVE_SUCCESS"
    assert repo["counts"]["runs_deleted"] == 2
    assert repo["counts"]["runs_already_absent"] == 1
    assert repo["counts"]["runs_failed"] == 0
    assert repo["counts"]["runs_remaining"] == 50
    assert repo["verification"]["status"] == "verified"
    assert 100000 in repo["verification"]["resolved_ids"]
    assert repo["verification"]["still_present_resolved_ids"] == []
    assert repo["verification"]["verified_already_absent_ids"] == [100000]
    assert repo["retention"]["runs_delete_404"] == 1
    assert repo["retention"]["runs_already_absent"] == 1
    assert len(record["delete_calls"]) == 3
    reports_dir = Path(env["FAKE_REPORTS_DIR"])
    markdown = (reports_dir / "reports" / f"{TARGET.replace('/', '__')}.md").read_text(encoding="utf-8")
    assert "- Deleted: 2" in markdown
    assert "- Already absent (idempotent): 1" in markdown


def test_workflow_delete_404_without_actual_absence_fails_verification(tmp_path):
    env, context, record_path, summary_path = _setup(
        tmp_path,
        "workflow_dispatch",
        {"mode": "live", "confirm_live": "DELETE", "repository": TARGET, "skip_branches": True},
    )
    env["FAKE_RUN_COUNT"] = "51"
    env["FAKE_404_WITHOUT_REMOVAL"] = json.dumps([100000])
    assert _resolve_mode(tmp_path, env, context).returncode == 0
    run = _run_cleanup(tmp_path, env, context)

    assert run.returncode == 1
    record = _record(record_path)
    repo = _repo_summary(record)
    assert repo["status"] == "FAILED"
    assert repo["counts"]["runs_deleted"] == 0
    # A raw DELETE 404 is not an already-absent success while the run is still
    # present in the complete re-list.
    assert repo["counts"]["runs_already_absent"] == 0
    assert repo["retention"]["runs_delete_404"] == 1
    assert repo["counts"]["runs_remaining"] == 51
    assert repo["verification"]["status"] == "mismatch"
    assert repo["verification"]["still_present_resolved_ids"] == [100000]
    assert repo["verification"]["verified_already_absent_ids"] == []
    assert any("verification mismatch" in failure for failure in repo["failures"])
    assert "FAILED" in summary_path.read_text(encoding="utf-8")

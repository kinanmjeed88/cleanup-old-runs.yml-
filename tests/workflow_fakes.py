"""Subprocess entry point for workflow tests: real CLI, fake GitHub transport.

This module is executed by a temporary ``python3`` shim placed on PATH.  It
never invokes ``gh`` or ``git`` against GitHub: the transport is an in-memory
FakeClient, while argument parsing, discovery, preflight, retention, deletion
execution and report writing are the production code paths.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.fakes import FakeBranchRunner, FakeClient, FakeWorkspaceFactory, build_runs
from tool import central_cleanup
from tool.github_client import ApiResult, GhCliClient

TARGET = "kinanmjeed88/Scan-id"
CENTRAL = "kinanmjeed88/cleanup-old-runs.yml-"


class FakeGhTransport:
    """Translate ``gh api`` commands into FakeClient calls (no network)."""

    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.calls: list[dict] = []

    def __call__(self, cmd: list[str], env: dict[str, str], timeout: int):
        assert cmd[:2] == ["gh", "api"], cmd
        # The production child environment must contain only the dedicated token.
        assert env.get("GH_TOKEN") == env.get("CLEANUP_TOKEN")
        assert "GITHUB_TOKEN" not in env
        url = urlsplit(cmd[2])
        method = cmd[cmd.index("--method") + 1]
        parts = url.path.strip("/").split("/")
        query = parse_qs(url.query)
        page = int(query.get("page", ["1"])[0])
        per_page = int(query.get("per_page", ["100"])[0])
        self.calls.append({"method": method, "endpoint": cmd[2]})

        if method == "DELETE" and len(parts) == 6 and parts[3:5] == ["actions", "runs"]:
            result = self.client.delete_workflow_run("/".join(parts[1:3]), int(parts[5]))
        elif method == "GET" and parts == ["user"]:
            result = ApiResult(True, 200, {"login": self.client.authenticated_login})
        elif method == "GET" and parts == ["user", "repos"]:
            result = self.client._page(
                self.client._owned_by(self.client.authenticated_login),
                page,
                per_page,
                include_permissions=True,
            )
        elif method == "GET" and len(parts) == 3 and parts[0] == "orgs" and parts[2] == "repos":
            result = self.client.list_org_repositories(parts[1], page=page, per_page=per_page)
        elif method == "GET" and len(parts) == 3 and parts[0] == "users" and parts[2] == "repos":
            result = self.client.list_user_repositories(parts[1], page=page, per_page=per_page)
        elif method == "GET" and len(parts) == 3 and parts[0] == "repos":
            result = self.client.get_repository("/".join(parts[1:3]))
        elif method == "GET" and len(parts) == 4 and parts[0] == "repos" and parts[3] == "pulls":
            result = self.client.list_open_pulls("/".join(parts[1:3]), page=page, per_page=per_page)
        elif method == "GET" and len(parts) == 5 and parts[0] == "repos" and parts[3:5] == ["actions", "runs"]:
            result = self.client.list_workflow_runs("/".join(parts[1:3]), page=page, per_page=per_page)
        else:
            raise AssertionError(f"unexpected fake API call: {method} {cmd[2]}")

        # Model the real `gh` CLI: a run-specific 404 exits non-zero even when
        # an abstract fake client chose to represent it as an idempotent result.
        # The production GhCliClient must recover the status, not see a 204.
        failed = (not result.ok) or result.status == 404
        return subprocess.CompletedProcess(
            cmd,
            returncode=1 if failed else 0,
            stdout="" if failed else (json.dumps(result.data) if result.data is not None else ""),
            stderr=(result.error or f"HTTP {result.status}") if failed else "",
        )


def main() -> int:
    argv = sys.argv[1:]
    assert argv and argv[0] == "tool/central_cleanup.py", argv
    record_path = Path(os.environ["FAKE_RECORD_PATH"])
    # Keep test artifacts out of the checkout while preserving the workflow's
    # real argument list in the record below.
    reports_dir = Path(os.environ["FAKE_REPORTS_DIR"])
    cli_argv = list(argv[1:])
    cli_argv[cli_argv.index("--reports-dir") + 1] = str(reports_dir)

    client = FakeClient()
    client.authenticated_login = "kinanmjeed88"
    run_count = int(os.environ.get("FAKE_RUN_COUNT", "739"))
    central_run_count = int(os.environ.get("FAKE_CENTRAL_RUN_COUNT", "60"))
    client.add_repository(TARGET, runs=build_runs(run_count, first_id=100000))
    client.add_repository(CENTRAL, runs=build_runs(central_run_count, first_id=300000))
    client.add_repository("techtouchAI/other", runs=build_runs(60, first_id=400000))
    if os.environ.get("FAKE_FIRST_DELETE_FORBIDDEN") == "true":
        client.forbid_delete.add(TARGET)

    # Deterministic HTTP/concurrency injections.  All state is in memory; no
    # request reaches GitHub.  Keys are run ids, values are HTTP statuses.
    base_delete = client.delete_workflow_run
    delete_failures = {
        int(run_id): int(status)
        for run_id, status in json.loads(os.environ.get("FAKE_DELETE_FAILURES", "{}")).items()
    }
    absent_on_delete = set(json.loads(os.environ.get("FAKE_ABSENT_ON_DELETE", "[]")))
    absent_without_removal = set(json.loads(os.environ.get("FAKE_404_WITHOUT_REMOVAL", "[]")))
    consumed_absent: set[int] = set()

    def delete_with_injections(repo, run_id):
        if run_id in absent_without_removal:
            client.delete_calls.append((repo, run_id))
            return ApiResult(False, 404, None, "gh: Not Found (HTTP 404)")
        if run_id in absent_on_delete and run_id not in consumed_absent:
            # Simulate another actor deleting the run between listing and DELETE.
            client.runs[repo] = [run for run in client.runs.get(repo, []) if run["id"] != run_id]
            consumed_absent.add(run_id)
        status = delete_failures.get(run_id)
        if status is not None:
            client.delete_calls.append((repo, run_id))
            return ApiResult(False, status, None, f"gh: injected deletion failure (HTTP {status})")
        return base_delete(repo, run_id)

    client.delete_workflow_run = delete_with_injections  # type: ignore[assignment]
    forbid_after = os.environ.get("FAKE_FORBID_DELETE_AFTER")
    if forbid_after is not None:
        delete_before_permission_loss = client.delete_workflow_run
        remaining = {"n": int(forbid_after)}

        def lose_permission(repo, run_id):
            if remaining["n"] <= 0:
                client.delete_calls.append((repo, run_id))
                return ApiResult(False, 403, None, "gh: Resource not accessible by integration (HTTP 403)")
            remaining["n"] -= 1
            return delete_before_permission_loss(repo, run_id)

        client.delete_workflow_run = lose_permission  # type: ignore[assignment]

    transport = FakeGhTransport(client)
    branch_runner = FakeBranchRunner()
    central_cleanup.GhCliClient = lambda **kwargs: GhCliClient(runner=transport, **kwargs)
    central_cleanup.GitWorkspaceFactory = lambda **kwargs: FakeWorkspaceFactory(reports_dir.parent / "fake-workspaces")
    central_cleanup.BranchCleanupRunner = lambda **kwargs: branch_runner

    exit_code = central_cleanup.main(cli_argv)
    summary_path = reports_dir / "summary.json"
    record = {
        "argv": argv[1:],
        "exit_code": exit_code,
        "api_calls": transport.calls,
        "delete_calls": client.delete_calls,
        "remaining_runs": {repo: len(runs) for repo, runs in client.runs.items()},
        "branch_calls": branch_runner.calls,
        "summary": json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else None,
    }
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

"""Test doubles: no network, no real deletions."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from tool.github_client import ApiResult


def iso(minutes_from_base: int, *, base: Optional[datetime] = None) -> str:
    anchor = base or datetime(2026, 1, 1, tzinfo=timezone.utc)
    return (anchor + timedelta(minutes=minutes_from_base)).strftime("%Y-%m-%dT%H:%M:%SZ")


def api_run(run_id: int, created_at: str, status: str = "completed", name: str = "") -> dict:
    return {
        "id": run_id,
        "created_at": created_at,
        "status": status,
        "name": name or f"run-{run_id}",
        "workflow_id": 10,
        "event": "push",
    }


def build_runs(count: int, *, first_id: int = 1000, status: str = "completed") -> list[dict]:
    """``count`` runs, id and timestamp increasing together (oldest first)."""
    return [api_run(first_id + index, iso(index), status=status) for index in range(count)]


@dataclass
class FakeClient:
    """In-memory GitHub API stand-in.

    ``runs`` maps repository -> list of raw run payloads (oldest first).
    """

    runs: dict[str, list[dict]] = field(default_factory=dict)
    repositories: dict[str, dict] = field(default_factory=dict)
    permissions: dict[str, dict] = field(default_factory=dict)
    fail_repos: set[str] = field(default_factory=set)
    fail_runs_page: dict[str, int] = field(default_factory=dict)
    forbid_runs: set[str] = field(default_factory=set)
    forbid_delete: set[str] = field(default_factory=set)
    open_pulls: dict[str, list[str]] = field(default_factory=dict)

    # discovery: "login" -> "User" | "Organization"
    accounts: dict[str, str] = field(default_factory=dict)
    authenticated_login: str = "owner"
    fail_accounts: set[str] = field(default_factory=set)

    list_calls: list[tuple[str, int]] = field(default_factory=list)
    delete_calls: list[tuple[str, int]] = field(default_factory=list)
    pull_calls: list[tuple[str, int]] = field(default_factory=list)
    discovery_calls: list[str] = field(default_factory=list)

    def add_repository(
        self,
        repo: str,
        *,
        runs: Iterable[dict] = (),
        can_push: bool = True,
        archived: bool = False,
        fork: bool = False,
        private: bool = False,
        owner_type: str = "User",
    ) -> None:
        owner = repo.split("/", 1)[0]
        self.accounts.setdefault(owner, owner_type)
        self.repositories[repo] = {
            "default_branch": "main",
            "archived": archived,
            "fork": fork,
            "private": private,
            "owner": {"login": owner, "type": owner_type},
            "full_name": repo,
        }
        if can_push:
            self.permissions[repo] = {
                "admin": False,
                "maintain": False,
                "push": True,
                "triage": True,
                "pull": True,
            }
        self.runs[repo] = list(runs)

    def _repo_payload(self, repo: str, *, include_permissions: bool) -> dict:
        payload = dict(self.repositories[repo])
        payload.setdefault("name", repo.split("/", 1)[1])
        payload.pop("permissions", None)
        if include_permissions and repo in self.permissions:
            payload["permissions"] = dict(self.permissions[repo])
        return payload

    def _owned_by(self, login: str) -> list[str]:
        return sorted(
            repo
            for repo in self.repositories
            if repo.split("/", 1)[0].lower() == login.lower()
        )

    def _page(self, repos: list[str], page: int, per_page: int, *, include_permissions: bool) -> ApiResult:
        start = (page - 1) * per_page
        window = repos[start : start + per_page]
        return ApiResult(
            True,
            200,
            [self._repo_payload(repo, include_permissions=include_permissions) for repo in window],
            None,
        )

    # -- API surface -----------------------------------------------------
    def get_repository(self, repo: str) -> ApiResult:
        if repo in self.fail_repos or repo not in self.repositories:
            return ApiResult(False, 404, None, "gh: Not Found (HTTP 404)")
        payload = dict(self.repositories[repo])
        payload["permissions"] = self.permissions.get(repo, {"push": False, "pull": True})
        return ApiResult(True, 200, payload, None)

    def list_open_pulls(self, repo: str, page: int = 1, per_page: int = 1) -> ApiResult:
        self.pull_calls.append((repo, page))
        if repo in self.fail_repos:
            return ApiResult(False, 404, None, "gh: Not Found (HTTP 404)")
        data = [{"number": 1, "head": {"ref": ref}} for ref in self.open_pulls.get(repo, [])]
        start = (page - 1) * per_page
        return ApiResult(True, 200, data[start : start + per_page], None)

    def list_workflow_runs(self, repo: str, page: int = 1, per_page: int = 100) -> ApiResult:
        self.list_calls.append((repo, page))
        if repo in self.fail_repos or repo in self.forbid_runs:
            status = 404 if repo in self.fail_repos else 403
            return ApiResult(False, status, None, f"gh: request failed (HTTP {status})")
        if self.fail_runs_page.get(repo) == page:
            return ApiResult(False, 500, None, "gh: Server Error (HTTP 500)")
        all_runs = self.runs.get(repo, [])
        start = (page - 1) * per_page
        window = all_runs[::-1][start : start + per_page]  # newest first, like GitHub
        return ApiResult(True, 200, {"total_count": len(all_runs), "workflow_runs": window}, None)

    # -- discovery -------------------------------------------------------
    def get_authenticated_user(self) -> ApiResult:
        self.discovery_calls.append("GET /user")
        return ApiResult(True, 200, {"login": self.authenticated_login, "type": "User"}, None)

    def list_authenticated_user_repos(
        self,
        page: int = 1,
        per_page: int = 100,
        affiliation: str = "owner,collaborator,organization_member",
    ) -> ApiResult:
        self.discovery_calls.append(f"GET /user/repos?page={page}")
        return self._page(self._owned_by(self.authenticated_login), page, per_page, include_permissions=True)

    def list_org_repositories(
        self, org: str, page: int = 1, per_page: int = 100, repo_type: str = "all"
    ) -> ApiResult:
        self.discovery_calls.append(f"GET /orgs/{org}/repos?page={page}")
        if org in self.fail_accounts:
            return ApiResult(False, 403, None, "gh: Forbidden (HTTP 403)")
        if self.accounts.get(org) != "Organization":
            return ApiResult(False, 404, None, "gh: Not Found (HTTP 404)")
        return self._page(self._owned_by(org), page, per_page, include_permissions=True)

    def list_user_repositories(
        self, owner: str, page: int = 1, per_page: int = 100, repo_type: str = "owner"
    ) -> ApiResult:
        self.discovery_calls.append(f"GET /users/{owner}/repos?page={page}")
        if owner in self.fail_accounts:
            return ApiResult(False, 403, None, "gh: Forbidden (HTTP 403)")
        if owner not in self.accounts:
            return ApiResult(False, 404, None, "gh: Not Found (HTTP 404)")
        # A public listing carries no `permissions` block.
        return self._page(self._owned_by(owner), page, per_page, include_permissions=False)

    def delete_workflow_run(self, repo: str, run_id: int) -> ApiResult:
        self.delete_calls.append((repo, run_id))
        if repo in self.forbid_delete:
            return ApiResult(False, 403, None, "gh: Resource not accessible by integration (HTTP 403)")
        remaining = [run for run in self.runs.get(repo, []) if run["id"] != run_id]
        if len(remaining) == len(self.runs.get(repo, [])):
            return ApiResult(False, 404, None, "gh: Not Found (HTTP 404)")
        self.runs[repo] = remaining
        return ApiResult(True, 204, None, None)


@dataclass
class FakeWorkspace:
    path: Path
    warnings: list[str] = field(default_factory=list)


class FakeWorkspaceFactory:
    def __init__(self, base: Path, *, error: Optional[Exception] = None) -> None:
        self.base = Path(base)
        self.error = error
        self.created: list[str] = []

    def create(self, repository: str):
        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            if self.error is not None:
                raise self.error
            self.created.append(repository)
            path = self.base / repository.replace("/", "__")
            path.mkdir(parents=True, exist_ok=True)
            yield FakeWorkspace(path=path)

        return _ctx()


@dataclass
class FakeBranchRunner:
    """Returns a canned branch report without touching git or the API."""

    decisions: dict[str, list[dict]] = field(default_factory=dict)
    report_override: Optional[dict] = None
    fail_for: set[str] = field(default_factory=set)
    calls: list[dict] = field(default_factory=list)

    def run(self, repository, *, live, workdir, report_path):  # noqa: ANN001
        from tool.central_cleanup import BranchRunResult

        self.calls.append(
            {"repository": repository, "live": live, "workdir": str(workdir), "report_path": str(report_path)}
        )
        if repository in self.fail_for:
            return BranchRunResult(ok=False, returncode=1, error="branch cleanup exited with code 1")
        payload = self.report_override
        if payload is None:
            decisions = self.decisions.get(repository, [])
            kept = sum(1 for item in decisions if item["action"] == "keep")
            deleted = sum(1 for item in decisions if item["action"] == "delete")
            payload = {
                "repo": repository,
                "dry_run": not live,
                "mode": "LIVE" if live else "DRY RUN",
                "default_branch": "main",
                "branches_examined": len(decisions),
                "kept": kept,
                "deleted": deleted if live else 0,
                "planned_deletions": deleted,
                "failed": 0,
                "results": decisions,
                "errors": [],
            }
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text(json.dumps(payload), encoding="utf-8")
        return BranchRunResult(ok=True, returncode=0, report=payload)

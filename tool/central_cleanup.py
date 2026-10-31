#!/usr/bin/env python3
"""Central multi-repository cleanup orchestrator with automatic discovery.

Every execution:

1. **Discovers** the repositories the cleanup token can manage
   (``tool/discovery.py``) and prints them before doing anything else.
2. **Preflights** the token per repository: Metadata read, Contents write,
   Pull requests read, Actions read. Nothing is mutated without write access.
3. **Keeps the newest N (default 20) completed workflow runs** per repository
   and deletes the older ones — ``tool/run_retention.py``.
4. **Deletes merged branches** with the unchanged decision algorithm in
   ``tool/branch_cleanup.py``, inside an isolated full clone of each repository.
   The central repository itself is excluded from branch cleanup.

Nothing is deleted unless ``--mode live --confirm-live DELETE`` is supplied and
the run is not a scheduled run. A repository that fails never stops the others.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow both `python3 tool/central_cleanup.py` and `python3 -m tool.central_cleanup`.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from tool.discovery import (
    DEFAULT_ACCOUNTS_RELATIVE_PATH,
    Account,
    DiscoveredRepository,
    DiscoveryResult,
    discover_repositories,
    load_accounts,
)
from tool.git_workspace import GitWorkspaceFactory, WorkspaceError
from tool.github_client import (
    TOKEN_ENV_VAR,
    CleanupTokenError,
    GhCliClient,
    build_child_env,
    redact,
    resolve_token,
)
from tool.reports import (
    MAX_LISTED_ITEMS,
    STATUS_ACCESSIBLE,
    STATUS_DRY_RUN,
    STATUS_FAILED,
    STATUS_LIVE,
    BranchDecision,
    RepoReport,
    ReportWriter,
    aggregate,
    utc_now_iso,
)
from tool.repositories import (
    DEFAULT_CONFIG_RELATIVE_PATH,
    RepositoryConfigError,
    is_valid_repository,
    load_repository_config,
)
from tool.run_retention import (
    DEFAULT_KEEP_LATEST,
    apply_retention,
    fetch_all_runs,
    plan_retention,
    summarise,
    validate_keep_latest,
)

EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_CONFIG = 2

LIVE_CONFIRMATION = "DELETE"

DISCOVERY_MODES = ("auto", "list", "union")


# ---------------------------------------------------------------------------
# Token preflight
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PermissionCheck:
    scope: str
    access: str  # "read" | "write"
    status: str  # "ok" | "missing" | "unverified"
    detail: str = ""

    def as_dict(self) -> dict:
        return {"scope": self.scope, "access": self.access, "status": self.status, "detail": self.detail}


@dataclass
class AccessReport:
    repository: str
    checks: list[PermissionCheck] = field(default_factory=list)
    fatal_error: Optional[str] = None
    fatal_status: Optional[int] = None
    can_manage_runs: bool = False
    can_manage_branches: bool = False
    contents_write: bool = False
    default_branch: Optional[str] = None
    owner_login: str = ""
    owner_type: str = ""
    private: bool = False
    archived: bool = False
    fork: bool = False
    disabled: bool = False

    def add(self, check: PermissionCheck) -> None:
        self.checks.append(check)

    def as_dicts(self) -> list[dict]:
        return [check.as_dict() for check in self.checks]


def check_repository_access(client: Any, repository: str) -> AccessReport:
    """Verify the token's capabilities on one repository (read-only probes).

    ``Actions: write`` cannot be proven without performing a deletion, so it is
    reported as ``unverified`` and enforced at the first deletion (a 403 aborts
    that repository).
    """
    access = AccessReport(repository=repository)

    repo_result = client.get_repository(repository)
    if not repo_result.ok:
        access.fatal_status = repo_result.status
        if repo_result.not_found:
            access.fatal_error = (
                "repository not found or not visible to the cleanup token (HTTP 404). "
                "A fine-grained token limited to selected repositories cannot see it."
            )
        elif repo_result.forbidden:
            access.fatal_error = (
                "the cleanup token was refused (HTTP 403) while reading repository metadata. "
                "The token requires `Metadata: Read` on this repository."
            )
        else:
            access.fatal_error = (
                f"cannot read repository metadata (status {repo_result.status}): {repo_result.error}"
            )
        access.add(PermissionCheck("metadata", "read", "missing", access.fatal_error))
        return access

    payload = repo_result.data if isinstance(repo_result.data, dict) else {}
    owner = payload.get("owner") if isinstance(payload.get("owner"), dict) else {}
    access.default_branch = payload.get("default_branch")
    access.owner_login = str(owner.get("login") or "")
    access.owner_type = str(owner.get("type") or "")
    access.private = bool(payload.get("private"))
    access.archived = bool(payload.get("archived"))
    access.fork = bool(payload.get("fork"))
    access.disabled = bool(payload.get("disabled"))
    access.add(PermissionCheck("metadata", "read", "ok", "repository metadata is readable"))

    permissions = payload.get("permissions") if isinstance(payload.get("permissions"), dict) else {}
    contents_write = bool(
        permissions.get("push") or permissions.get("admin") or permissions.get("maintain")
    )
    access.contents_write = contents_write
    access.add(
        PermissionCheck(
            "contents",
            "write",
            "ok" if contents_write else "missing",
            "token can push to the repository"
            if contents_write
            else "token has no write access (read-only); deletions require `Contents: Read and write`",
        )
    )

    pulls_result = client.list_open_pulls(repository, page=1, per_page=1)
    pulls_ok = pulls_result.ok
    access.add(
        PermissionCheck(
            "pull-requests",
            "read",
            "ok" if pulls_ok else "missing",
            "open pull requests are readable"
            if pulls_ok
            else f"cannot read pull requests (status {pulls_result.status}): {pulls_result.error}",
        )
    )

    runs_result = client.list_workflow_runs(repository, page=1, per_page=1)
    runs_ok = runs_result.ok
    access.add(
        PermissionCheck(
            "actions",
            "read",
            "ok" if runs_ok else "missing",
            "workflow runs are readable"
            if runs_ok
            else f"cannot read workflow runs (status {runs_result.status}): {runs_result.error}",
        )
    )
    access.add(
        PermissionCheck(
            "actions",
            "write",
            "unverified",
            "write access cannot be probed without deleting a run; "
            "it is verified at the first deletion and a 403 aborts the repository",
        )
    )

    access.can_manage_runs = runs_ok
    access.can_manage_branches = contents_write and pulls_ok
    return access


# ---------------------------------------------------------------------------
# Branch cleanup subprocess
# ---------------------------------------------------------------------------


@dataclass
class BranchRunResult:
    ok: bool
    returncode: int
    report: dict = field(default_factory=dict)
    error: Optional[str] = None
    log_tail: str = ""


class BranchCleanupRunner:
    """Runs ``tool/branch_cleanup.py`` inside an isolated clone directory."""

    def __init__(
        self,
        *,
        token: str,
        script_path: str | Path,
        python_executable: Optional[str] = None,
        timeout: int = 1800,
        runner: Optional[Callable[..., Any]] = None,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.token = token
        # Resolve once: the child runs with a different working directory.
        self.script_path = Path(script_path).resolve()
        self.python_executable = python_executable or sys.executable
        self.timeout = timeout
        self._runner = runner
        self._log = log or (lambda msg: print(msg))

    def _child_env(self) -> dict[str, str]:
        env = build_child_env(
            self.token,
            extra={
                "PYTHONUNBUFFERED": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
        # The child must never inherit workflow-level switches or summary sinks.
        env.pop("GITHUB_STEP_SUMMARY", None)
        env.pop("DELETE_BRANCHES", None)
        return env

    def run(
        self,
        repository: str,
        *,
        live: bool,
        workdir: str | Path,
        report_path: str | Path,
    ) -> BranchRunResult:
        # Never hand a relative path to a process with a different cwd.
        report_path = Path(report_path).resolve()
        if not self.script_path.is_file():
            return BranchRunResult(
                ok=False,
                returncode=-1,
                error=f"branch cleanup script not found: {self.script_path}",
            )

        command = [
            self.python_executable,
            str(self.script_path),
            "--repo",
            repository,
            "--skip-runs",
            "--json-report",
            str(report_path),
        ]
        if live:
            command.append("--delete")

        runner = self._runner
        if runner is None:
            import subprocess

            runner = lambda cmd, **kwargs: subprocess.run(cmd, check=False, **kwargs)  # noqa: E731

        try:
            proc = runner(
                command,
                capture_output=True,
                text=True,
                cwd=str(workdir),
                env=self._child_env(),
                timeout=self.timeout,
            )
        except Exception as exc:  # pragma: no cover - environment specific
            return BranchRunResult(
                ok=False,
                returncode=-1,
                error=f"could not execute the branch cleanup script: {redact(str(exc), self.token)}",
            )

        stdout = redact(getattr(proc, "stdout", "") or "", self.token)
        stderr = redact(getattr(proc, "stderr", "") or "", self.token)
        for line in stdout.splitlines():
            self._log(f"    {line}")
        for line in stderr.splitlines():
            self._log(f"    {line}")

        payload: dict = {}
        report_file = Path(report_path)
        if report_file.exists():
            try:
                payload = json.loads(report_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                payload = {}
                stderr += f"\nunreadable branch report: {exc}"

        log_tail = "\n".join((stdout + "\n" + stderr).strip().splitlines()[-40:])
        if proc.returncode != 0:
            return BranchRunResult(
                ok=False,
                returncode=proc.returncode,
                report=payload,
                error=f"branch cleanup exited with code {proc.returncode}",
                log_tail=log_tail,
            )
        if not payload:
            return BranchRunResult(
                ok=False,
                returncode=proc.returncode,
                report={},
                error="branch cleanup produced no JSON report",
                log_tail=log_tail,
            )
        return BranchRunResult(ok=True, returncode=0, report=payload, log_tail=log_tail)


# ---------------------------------------------------------------------------
# Options / orchestration
# ---------------------------------------------------------------------------


@dataclass
class CleanupOptions:
    mode: str = "dry-run"  # "dry-run" | "live"
    keep_latest: int = DEFAULT_KEEP_LATEST
    run_runs: bool = True
    run_branches: bool = True
    central_repository: str = ""
    current_run_id: str = ""
    branch_exclusions: frozenset[str] = frozenset()
    discovery_mode: str = "auto"

    def __post_init__(self) -> None:
        self.keep_latest = validate_keep_latest(self.keep_latest)
        if self.mode not in ("dry-run", "live"):
            raise ValueError("mode must be 'dry-run' or 'live'")
        if self.discovery_mode not in DISCOVERY_MODES:
            raise ValueError(f"discovery_mode must be one of {DISCOVERY_MODES}")

    @property
    def live(self) -> bool:
        return self.mode == "live"

    @property
    def mode_label(self) -> str:
        return "LIVE" if self.live else "DRY RUN"

    def branch_cleanup_excluded(self, repository: str) -> bool:
        wanted = repository.lower()
        if self.central_repository and wanted == self.central_repository.lower():
            return True
        return wanted in {entry.lower() for entry in self.branch_exclusions}


class CentralCleanup:
    def __init__(
        self,
        *,
        client: Any,
        workspace_factory: Any,
        branch_runner: Any,
        writer: ReportWriter,
        options: CleanupOptions,
        discovery: Optional[DiscoveryResult] = None,
        raw_report_dir: str | Path = "diagnostics/raw",
        log: Optional[Callable[[str], None]] = None,
        token: str = "",
    ) -> None:
        self.client = client
        self.workspace_factory = workspace_factory
        self.branch_runner = branch_runner
        self.writer = writer
        self.options = options
        self.discovery = discovery
        # Absolute: the branch cleanup child process runs inside a throw-away
        # clone, so a relative path would be written into (and deleted with) it.
        self.raw_report_dir = Path(raw_report_dir).resolve()
        self.token = token
        self._emit = log or (lambda msg: print(msg))

    def log(self, message: str) -> None:
        self._emit(redact(message, self.token))

    # -- single repository ------------------------------------------------
    def process(self, repository: DiscoveredRepository | str) -> RepoReport:
        meta = (
            repository
            if isinstance(repository, DiscoveredRepository)
            else DiscoveredRepository(full_name=str(repository), owner_login="", owner_type="")
        )
        report = RepoReport(
            repository=meta.full_name,
            mode=self.options.mode_label,
            keep_latest=self.options.keep_latest,
            owner_login=meta.owner_login,
            owner_type=meta.owner_type,
            visibility=meta.visibility if meta.owner_login else "",
            default_branch=meta.default_branch or None,
            archived=meta.archived,
            fork=meta.fork,
            discovered_from=list(meta.sources),
        )
        self.log(f"=== {meta.full_name} [{self.options.mode_label}] ===")

        try:
            access = check_repository_access(self.client, meta.full_name)
        except Exception as exc:
            report.add_failure(f"token preflight failed: {exc.__class__.__name__}: {exc}")
            return report

        if access.owner_login:
            report.owner_login = access.owner_login
            report.owner_type = access.owner_type
        if access.default_branch:
            report.default_branch = access.default_branch
        report.visibility = "private" if access.private else ("public" if access.fatal_error is None else "")
        report.archived = access.archived or meta.archived
        report.fork = access.fork or meta.fork
        report.permissions = access.as_dicts()
        for check in access.checks:
            if check.status == "missing":
                report.add_note(f"missing permission: {check.scope}:{check.access} — {check.detail}")
            elif check.status == "unverified":
                report.add_note(f"unverified permission: {check.scope}:{check.access} — {check.detail}")

        if access.fatal_error:
            report.skip(access.fatal_error)
            self.log(f"::warning::{meta.full_name}: {access.fatal_error}")
            return report

        if report.archived:
            report.skip("archived repository — deletions are never performed on archived repositories")
            self.log(f"{meta.full_name}: archived — skipped")
            return report

        writable = access.contents_write
        if not writable:
            report.add_note(
                "token has read-only access: retention is analysed but nothing is deleted, and the "
                "branch phase is skipped because no branch could be removed"
            )

        executed_any_phase = False

        if self.options.run_runs:
            if not access.can_manage_runs:
                report.add_failure(
                    "skipped workflow-run retention: `Actions: Read` is not available to the cleanup token"
                )
            else:
                self._process_runs(meta.full_name, report, dry_run=not (self.options.live and writable))
                executed_any_phase = True

        if self.options.run_branches:
            if self.options.branch_cleanup_excluded(meta.full_name):
                note = (
                    "branch cleanup excluded for the central repository"
                    if self.options.central_repository
                    and meta.full_name.lower() == self.options.central_repository.lower()
                    else "branch cleanup excluded by configuration"
                )
                report.add_note(note)
                self.log(f"{meta.full_name}: {note}")
            elif not access.can_manage_branches:
                report.add_note(
                    "branch cleanup skipped: requires `Contents: Read and write` and `Pull requests: Read`"
                )
            elif not writable:
                report.add_note("branch cleanup skipped: read-only token")
            else:
                self._process_branches(meta.full_name, report)
                executed_any_phase = True

        # Final status: failures win, then exclusions, then the executed mode.
        if report.failures:
            report.status = STATUS_FAILED
        elif not writable:
            report.skip(
                "read-only access: nothing can be deleted with this token "
                "(needs `Actions: Read and write` and `Contents: Read and write`)"
            )
        elif self.options.live:
            report.status = STATUS_LIVE
        elif executed_any_phase:
            report.status = STATUS_DRY_RUN
        else:
            report.status = STATUS_ACCESSIBLE

        return report

    # -- runs -------------------------------------------------------------
    def _process_runs(self, repository: str, report: RepoReport, *, dry_run: bool) -> None:
        listing = fetch_all_runs(self.client, repository)
        current_run_id = ""
        if (
            self.options.current_run_id
            and self.options.central_repository
            and repository.lower() == self.options.central_repository.lower()
        ):
            current_run_id = self.options.current_run_id

        plan = plan_retention(
            listing.runs,
            repository=repository,
            keep_latest=self.options.keep_latest,
            current_run_id=current_run_id,
            complete=listing.complete,
            errors=listing.errors,
        )
        outcome = apply_retention(self.client, repository, plan, dry_run=dry_run, log=self.log)

        report.runs_examined = plan.examined
        report.runs_retained = len(plan.keep)
        report.runs_deleted = len(outcome.deleted)
        report.runs_planned_deletions = len(outcome.planned)
        report.runs_excluded = len(plan.excluded)
        report.runs_failed = len(outcome.failed)
        report.retention = summarise(plan, outcome)
        report.excluded_runs = [item.as_dict() for item in plan.excluded[:MAX_LISTED_ITEMS]]

        for error in listing.errors:
            report.add_failure(error)
        if outcome.aborted_reason and not listing.errors:
            report.add_note(f"retention aborted: {outcome.aborted_reason}")
        if outcome.stop_reason:
            report.add_failure(outcome.stop_reason)
        for run_id, message in outcome.failed:
            report.add_failure(f"run {run_id}: {message}")

        self.log(
            f"Runs: examined={report.runs_examined} retained={report.runs_retained} "
            f"deleted={report.runs_deleted} planned={report.runs_planned_deletions} "
            f"excluded={report.runs_excluded} failed={report.runs_failed}"
        )

    # -- branches ---------------------------------------------------------
    def _process_branches(self, repository: str, report: RepoReport) -> None:
        slug = repository.replace("/", "__")
        report_path = self.raw_report_dir / f"{slug}.branches.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            with self.workspace_factory.create(repository) as workspace:
                result = self.branch_runner.run(
                    repository,
                    live=self.options.live,
                    workdir=workspace.path,
                    report_path=report_path,
                )
                for warning in getattr(workspace, "warnings", []) or []:
                    report.add_note(warning)
        except WorkspaceError as exc:
            report.add_failure(f"could not create an isolated git workspace: {exc}")
            return
        except Exception as exc:
            report.add_failure(f"branch cleanup failed: {exc.__class__.__name__}: {exc}")
            return

        payload = result.report or {}
        report.branches_examined = int(payload.get("branches_examined") or 0)
        report.branches_kept = int(payload.get("kept") or 0)
        report.branches_deleted = int(payload.get("deleted") or 0)
        report.branches_planned_deletions = int(payload.get("planned_deletions") or 0)
        report.branches_failed = int(payload.get("failed") or 0)
        if payload.get("default_branch"):
            report.default_branch = payload["default_branch"]
        report.branch_decisions = [
            BranchDecision(
                branch=str(item.get("branch", "")),
                action=str(item.get("action", "")),
                reason=str(item.get("reason", "")),
            )
            for item in payload.get("results", [])
            if isinstance(item, dict)
        ]

        if not result.ok:
            report.add_failure(result.error or "branch cleanup failed")
        for error in payload.get("errors", []) or []:
            report.add_failure(str(error))
        if report.branches_failed and not payload.get("errors"):
            report.add_failure(f"{report.branches_failed} branch deletion(s) failed")

        self.log(
            f"Branches: examined={report.branches_examined} kept={report.branches_kept} "
            f"deleted={report.branches_deleted} planned={report.branches_planned_deletions} "
            f"failed={report.branches_failed}"
        )

    # -- all repositories --------------------------------------------------
    def run(self, repositories: Sequence[DiscoveredRepository | str]) -> list[RepoReport]:
        reports: list[RepoReport] = []
        total = len(repositories)
        for index, repository in enumerate(repositories, start=1):
            name = (
                repository.full_name
                if isinstance(repository, DiscoveredRepository)
                else str(repository)
            )
            self.log(f"--- [{index}/{total}] {name} ---")
            try:
                report = self.process(repository)
            except Exception as exc:  # one repository must never abort the rest
                report = RepoReport(repository=name, mode=self.options.mode_label)
                report.add_failure(f"unexpected error: {exc.__class__.__name__}: {exc}")
                self.log(f"::error::{name}: unexpected error {exc.__class__.__name__}")
            report.finished_at = utc_now_iso()
            self.writer.write_repository(report)
            reports.append(report)

        discovery_payload = self.discovery.as_dict() if self.discovery else None
        self.writer.write_aggregate(reports, discovery=discovery_payload)
        self.writer.append_step_summary(reports, discovery=discovery_payload)
        return reports


# ---------------------------------------------------------------------------
# Discovery presentation / target selection
# ---------------------------------------------------------------------------


def print_discovery(discovery: DiscoveryResult, log: Callable[[str], None]) -> None:
    log("")
    log("=" * 110)
    log(
        f"DISCOVERED REPOSITORIES: {discovery.count} "
        f"(authenticated as {discovery.authenticated_user or 'unknown'}, "
        f"complete={'yes' if discovery.complete else 'NO'})"
    )
    log("=" * 110)
    for source in discovery.sources:
        state = "ok" if source.complete else "INCOMPLETE"
        if source.skipped:
            state = "skipped"
        log(
            f"  source {source.endpoint:<52} repos={source.repositories:<4} "
            f"pages={source.pages_read:<3} {state}"
        )
        if source.error:
            log(f"      -> {source.error}")
    log("")
    header = f"  {'repository':<44} {'owner':<16} {'type':<13} {'vis':<8} {'arch':<5} {'fork':<5} write"
    log(header)
    log("  " + "-" * (len(header) - 2))
    for repo in discovery.repositories:
        log(
            f"  {repo.full_name:<44} {repo.owner_login:<16} {repo.owner_type:<13} "
            f"{repo.visibility:<8} {'yes' if repo.archived else 'no':<5} "
            f"{'yes' if repo.fork else 'no':<5} {'yes' if repo.can_write else 'no'}"
        )
    for note in discovery.notes:
        log(f"NOTE: {note}")
    for error in discovery.errors:
        log(f"::warning::discovery: {error}")
    log("")


def select_targets(
    discovery: DiscoveryResult,
    config_repositories: Sequence[str],
    *,
    discovery_mode: str,
    selector: str = "all",
    limit: int = 0,
) -> list[DiscoveredRepository]:
    """Choose the repositories to process, never inventing an unknown target."""
    targets: dict[str, DiscoveredRepository] = {}
    if discovery_mode in ("auto", "union"):
        for repo in discovery.repositories:
            targets[repo.key] = repo
    if discovery_mode in ("list", "union"):
        for full_name in config_repositories:
            existing = discovery.get(full_name)
            if existing is not None:
                targets.setdefault(existing.key, existing)
                continue
            # list/union: an explicitly configured repository is a target even
            # when the token's listing endpoints did not return it. The token
            # preflight still has to prove access before anything is touched.
            owner = full_name.split("/", 1)[0]
            targets[full_name.lower()] = DiscoveredRepository(
                full_name=full_name,
                owner_login=owner,
                owner_type="",
                sources=("config-list",),
            )

    selected = sorted(targets.values(), key=lambda repo: repo.key)
    value = (selector or "all").strip()
    if value and value.lower() != "all":
        if not is_valid_repository(value):
            raise RepositoryConfigError(
                f"invalid repository selector {selector!r}; expected `owner/name` or `all`"
            )
        match = [repo for repo in selected if repo.key == value.lower()]
        if not match:
            raise RepositoryConfigError(
                f"repository {value!r} was not discovered by the token in {discovery_mode!r} mode; "
                "refusing to operate on an undiscovered repository"
            )
        selected = match
    if limit and limit > 0:
        selected = selected[:limit]
    return selected


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Central cleanup: discovers every repository the token can manage, keeps the "
        "newest completed workflow runs and deletes merged branches.",
    )
    parser.add_argument(
        "--discovery-mode",
        choices=DISCOVERY_MODES,
        default="auto",
        help="auto = every discovered repository (default); list = config/repositories.txt only; "
        "union = discovered plus the list",
    )
    parser.add_argument(
        "--accounts",
        default=DEFAULT_ACCOUNTS_RELATIVE_PATH,
        help="accounts file used for discovery (default config/accounts.txt)",
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG_RELATIVE_PATH, help="repository list path")
    parser.add_argument("--mode", choices=("dry-run", "live"), default="dry-run")
    parser.add_argument(
        "--confirm-live",
        default="",
        help=f"required with --mode live; must be exactly {LIVE_CONFIRMATION}",
    )
    parser.add_argument(
        "--repository",
        default="all",
        help="`all` or one discovered repository (`owner/name`)",
    )
    parser.add_argument(
        "--keep-latest",
        default=str(DEFAULT_KEEP_LATEST),
        help="completed workflow runs to keep per repository (default 20)",
    )
    parser.add_argument("--reports-dir", default="diagnostics", help="directory for reports")
    parser.add_argument(
        "--discover-only", action="store_true", help="discover and report, then stop (no cleanup)"
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="process at most N repositories (0 = all)"
    )
    parser.add_argument("--skip-runs", action="store_true", help="skip workflow-run retention")
    parser.add_argument("--skip-branches", action="store_true", help="skip branch cleanup")
    parser.add_argument(
        "--exclude-branch-cleanup",
        action="append",
        default=[],
        metavar="OWNER/NAME",
        help="never delete branches in this repository (repeatable; the central repository is "
        "always excluded)",
    )
    parser.add_argument(
        "--central-repository",
        default=os.environ.get("GITHUB_REPOSITORY", ""),
        help="owner/name of this central repository (branch cleanup is excluded for it)",
    )
    parser.add_argument(
        "--current-run-id",
        default=os.environ.get("GITHUB_RUN_ID", ""),
        help="run id of the current cleanup execution",
    )
    parser.add_argument("--repo-root", default=".", help="repository root used to confine config")
    parser.add_argument(
        "--workspace-dir",
        default="",
        help="optional directory for isolated clones (default: system temp directory)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    # ---- live-mode gating (defense in depth, independent of the workflow) ----
    if args.mode == "live":
        if args.confirm_live != LIVE_CONFIRMATION:
            print(
                f"::error::live mode requires --confirm-live {LIVE_CONFIRMATION}; "
                "no deletions were performed",
                file=sys.stderr,
            )
            return EXIT_CONFIG
        if os.environ.get("GITHUB_EVENT_NAME", "") == "schedule":
            print(
                "::error::scheduled runs are preview-only; live cleanup must be dispatched manually. "
                "No deletions were performed.",
                file=sys.stderr,
            )
            return EXIT_CONFIG

    try:
        token = resolve_token(env_var=TOKEN_ENV_VAR)
    except CleanupTokenError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return EXIT_CONFIG

    exclusions = {entry for entry in args.exclude_branch_cleanup if entry}
    try:
        keep_latest = int(str(args.keep_latest).strip())
        options = CleanupOptions(
            mode=args.mode,
            keep_latest=keep_latest,
            run_runs=not args.skip_runs,
            run_branches=not args.skip_branches,
            central_repository=args.central_repository,
            current_run_id=args.current_run_id,
            branch_exclusions=frozenset(exclusions),
            discovery_mode=args.discovery_mode,
        )
    except ValueError as exc:
        print(f"::error::invalid input: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    if args.limit < 0:
        print("::error::--limit must be zero or positive", file=sys.stderr)
        return EXIT_CONFIG
    selector = (args.repository or "all").strip()
    if selector.lower() != "all" and not is_valid_repository(selector):
        print(
            f"::error::invalid --repository value {args.repository!r}; expected `owner/name` or `all`",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    for entry in args.exclude_branch_cleanup:
        if not is_valid_repository(entry):
            print(f"::error::invalid --exclude-branch-cleanup value {entry!r}", file=sys.stderr)
            return EXIT_CONFIG

    # ---- accounts ----
    accounts: tuple[Account, ...] = ()
    account_warnings: list[str] = []
    if args.accounts:
        try:
            account_list = load_accounts(args.accounts, repo_root=args.repo_root)
        except RepositoryConfigError as exc:
            account_warnings.append(f"accounts file not usable ({exc}); discovering /user/repos only")
        else:
            if account_list.errors:
                print("::error::the accounts file contains invalid entries:", file=sys.stderr)
                for error in account_list.errors:
                    print(f"  - {error}", file=sys.stderr)
                return EXIT_CONFIG
            accounts = account_list.accounts

    # ---- repository list (only used by list/union modes) ----
    config_repositories: tuple[str, ...] = ()
    if args.discovery_mode in ("list", "union"):
        try:
            config = load_repository_config(args.config, repo_root=args.repo_root)
        except RepositoryConfigError as exc:
            print(f"::error::{exc}", file=sys.stderr)
            return EXIT_CONFIG
        if config.errors:
            print("::error::the repository list contains invalid entries:", file=sys.stderr)
            for error in config.errors:
                print(f"  - {error}", file=sys.stderr)
            return EXIT_CONFIG
        config_repositories = config.repositories

    log = lambda message: print(redact(message, token))  # noqa: E731
    for warning in account_warnings:
        log(f"::warning::{warning}")

    repo_root = Path(args.repo_root).resolve()
    script_path = repo_root / "tool" / "branch_cleanup.py"
    if not args.skip_branches and not script_path.is_file():
        print(f"::error::branch cleanup script not found: {script_path}", file=sys.stderr)
        return EXIT_CONFIG

    client = GhCliClient(token=token)

    # ---- discovery (always first) ----
    discovery = discover_repositories(client, accounts)
    print_discovery(discovery, log)

    reports_dir = Path(args.reports_dir).resolve()
    writer = ReportWriter(reports_dir, token=token, mode=options.mode_label)

    if not discovery.repositories:
        print("::error::discovery found no repositories; nothing to do", file=sys.stderr)
        writer.write_aggregate([], discovery=discovery.as_dict())
        return EXIT_CONFIG

    if args.discover_only:
        writer.write_aggregate([], discovery=discovery.as_dict())
        log(
            f"Discovery only: {discovery.count} repositories, "
            f"complete={'yes' if discovery.complete else 'NO'}. No cleanup performed."
        )
        return EXIT_OK if discovery.complete else EXIT_FAILURES

    if options.live and not discovery.complete:
        print(
            "::error::discovery is incomplete, so the repository set cannot be trusted; "
            "live deletion refused. Fix the discovery errors and re-run the dry run.",
            file=sys.stderr,
        )
        writer.write_aggregate([], discovery=discovery.as_dict())
        return EXIT_CONFIG

    try:
        selected = select_targets(
            discovery,
            config_repositories,
            discovery_mode=args.discovery_mode,
            selector=args.repository,
            limit=args.limit,
        )
    except RepositoryConfigError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return EXIT_CONFIG
    if not selected:
        print("::error::no repositories selected for processing", file=sys.stderr)
        return EXIT_CONFIG

    workspace_factory = GitWorkspaceFactory(
        token=token,
        base_dir=args.workspace_dir or None,
        log=log,
    )
    branch_runner = BranchCleanupRunner(token=token, script_path=script_path, log=log)

    cleanup = CentralCleanup(
        client=client,
        workspace_factory=workspace_factory,
        branch_runner=branch_runner,
        writer=writer,
        options=options,
        discovery=discovery,
        raw_report_dir=reports_dir / "raw",
        log=log,
        token=token,
    )

    log(
        f"Central cleanup starting — mode={options.mode_label} keep_latest={options.keep_latest} "
        f"discovered={discovery.count} selected={len(selected)} "
        f"runs={'on' if options.run_runs else 'off'} branches={'on' if options.run_branches else 'off'}"
    )
    reports = cleanup.run(selected)

    failed = [report for report in reports if report.status == STATUS_FAILED]
    totals = aggregate(reports)
    log(
        "Central cleanup finished — "
        f"repositories={len(reports)} failed={len(failed)} skipped={totals['repositories_skipped']} "
        f"runs_deleted={totals['runs_deleted']} runs_planned={totals['runs_planned_deletions']} "
        f"branches_deleted={totals['branches_deleted']} "
        f"branches_planned={totals['branches_planned_deletions']}"
    )
    if not options.live:
        log("DRY RUN: nothing was deleted.")

    for report in failed:
        log(f"::error::{report.repository}: {report.failure_count} failure(s); see diagnostics/reports/")

    return EXIT_FAILURES if failed else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

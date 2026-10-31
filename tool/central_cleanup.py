#!/usr/bin/env python3
"""Central multi-repository cleanup orchestrator.

For every repository listed in ``config/repositories.txt`` (independently):

1. Preflight the dedicated cleanup token (``CLEANUP_TOKEN``) against the
   repository: Metadata read, Contents write, Pull requests read, Actions read.
   A repository that fails the preflight is reported and skipped; the remaining
   repositories are still processed.
2. Keep the newest N (default 20) *completed* workflow runs and delete the older
   ones — see ``tool/run_retention.py``.
3. Delete merged, unprotected, PR-free branches using the **unchanged**
   decision algorithm in ``tool/branch_cleanup.py``, executed inside an isolated
   full clone of the target repository.

Nothing is deleted unless ``--mode live --confirm-live DELETE`` is supplied and
the run is not a scheduled run.
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

from tool.git_workspace import GitWorkspaceFactory, WorkspaceError
from tool.github_client import (
    TOKEN_ENV_VAR,
    CleanupTokenError,
    GhCliClient,
    build_child_env,
    redact,
    resolve_token,
)
from tool.reports import MAX_LISTED_ITEMS, BranchDecision, RepoReport, ReportWriter, aggregate, utc_now_iso
from tool.repositories import (
    DEFAULT_CONFIG_RELATIVE_PATH,
    RepositoryConfigError,
    RepositoryList,
    load_repository_config,
    select_repositories,
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
    can_manage_runs: bool = False
    can_manage_branches: bool = False
    default_branch: Optional[str] = None

    def add(self, check: PermissionCheck) -> None:
        self.checks.append(check)

    def as_dicts(self) -> list[dict]:
        return [check.as_dict() for check in self.checks]


def check_repository_access(client: Any, repository: str) -> AccessReport:
    """Verify the token's capabilities on one target repository.

    Only read-only probes are used. ``Actions: write`` cannot be proven without
    performing a deletion, so it is reported as ``unverified`` and enforced at
    the first deletion (a 403 aborts that repository).
    """
    access = AccessReport(repository=repository)

    repo_result = client.get_repository(repository)
    if not repo_result.ok:
        if repo_result.not_found:
            access.fatal_error = (
                "repository not found or not visible to the cleanup token (HTTP 404). "
                "Verify the name in config/repositories.txt and that the token can access it."
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
    access.default_branch = payload.get("default_branch")
    access.add(PermissionCheck("metadata", "read", "ok", "repository metadata is readable"))

    permissions = payload.get("permissions") if isinstance(payload.get("permissions"), dict) else {}
    contents_write = bool(
        permissions.get("push") or permissions.get("admin") or permissions.get("maintain")
    )
    access.add(
        PermissionCheck(
            "contents",
            "write",
            "ok" if contents_write else "missing",
            "token can push to the repository"
            if contents_write
            else "token has no write access; branch deletion requires `Contents: Read and write`",
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

    def __post_init__(self) -> None:
        self.keep_latest = validate_keep_latest(self.keep_latest)
        if self.mode not in ("dry-run", "live"):
            raise ValueError("mode must be 'dry-run' or 'live'")

    @property
    def live(self) -> bool:
        return self.mode == "live"

    @property
    def mode_label(self) -> str:
        return "LIVE" if self.live else "DRY RUN"


class CentralCleanup:
    def __init__(
        self,
        *,
        client: Any,
        workspace_factory: Any,
        branch_runner: Any,
        writer: ReportWriter,
        options: CleanupOptions,
        raw_report_dir: str | Path = "diagnostics/raw",
        log: Optional[Callable[[str], None]] = None,
        token: str = "",
    ) -> None:
        self.client = client
        self.workspace_factory = workspace_factory
        self.branch_runner = branch_runner
        self.writer = writer
        self.options = options
        self.raw_report_dir = Path(raw_report_dir)
        self.token = token
        self._emit = log or (lambda msg: print(msg))

    def log(self, message: str) -> None:
        self._emit(redact(message, self.token))

    # -- single repository ------------------------------------------------
    def process(self, repository: str) -> RepoReport:
        report = RepoReport(
            repository=repository,
            mode=self.options.mode_label,
            keep_latest=self.options.keep_latest,
        )
        self.log(f"=== {repository} [{self.options.mode_label}] ===")

        try:
            access = check_repository_access(self.client, repository)
        except Exception as exc:
            report.add_failure(f"token preflight failed: {exc.__class__.__name__}: {exc}")
            return report

        report.default_branch = access.default_branch
        report.permissions = access.as_dicts()
        for check in access.checks:
            if check.status == "missing":
                report.add_note(f"missing permission: {check.scope}:{check.access} — {check.detail}")
            elif check.status == "unverified":
                report.add_note(f"unverified permission: {check.scope}:{check.access} — {check.detail}")

        if access.fatal_error:
            report.add_failure(access.fatal_error)
            self.log(f"::error::{repository}: {access.fatal_error}")
            return report

        if self.options.run_runs:
            if not access.can_manage_runs:
                report.add_failure(
                    "skipped workflow-run retention: `Actions: Read` is not available to the cleanup token"
                )
            else:
                self._process_runs(repository, report)

        if self.options.run_branches:
            if not access.can_manage_branches:
                report.add_failure(
                    "skipped branch cleanup: `Contents: Read and write` and `Pull requests: Read` "
                    "are both required"
                )
            else:
                self._process_branches(repository, report)

        return report

    # -- runs -------------------------------------------------------------
    def _process_runs(self, repository: str, report: RepoReport) -> None:
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
        outcome = apply_retention(
            self.client,
            repository,
            plan,
            dry_run=not self.options.live,
            log=self.log,
        )

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
    def run(self, repositories: Sequence[str]) -> list[RepoReport]:
        reports: list[RepoReport] = []
        for repository in repositories:
            try:
                report = self.process(repository)
            except Exception as exc:  # one repository must never abort the rest
                report = RepoReport(
                    repository=repository,
                    mode=self.options.mode_label,
                    keep_latest=self.options.keep_latest,
                )
                report.add_failure(f"unexpected error: {exc.__class__.__name__}: {exc}")
                self.log(f"::error::{repository}: unexpected error {exc.__class__.__name__}")
            report.finished_at = utc_now_iso()
            self.writer.write_repository(report)
            reports.append(report)

        self.writer.write_aggregate(reports)
        self.writer.append_step_summary(reports)
        return reports


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Central cleanup: keep the newest completed workflow runs and delete merged branches "
        "across the repositories listed in config/repositories.txt.",
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
        help="`all` or one repository from the configured list",
    )
    parser.add_argument(
        "--keep-latest",
        default=str(DEFAULT_KEEP_LATEST),
        help="completed workflow runs to keep per repository (default 20)",
    )
    parser.add_argument("--reports-dir", default="diagnostics", help="directory for reports")
    parser.add_argument("--skip-runs", action="store_true", help="skip workflow-run retention")
    parser.add_argument("--skip-branches", action="store_true", help="skip branch cleanup")
    parser.add_argument(
        "--central-repository",
        default=os.environ.get("GITHUB_REPOSITORY", ""),
        help="owner/name of this central repository (its own run id is never deleted)",
    )
    parser.add_argument(
        "--current-run-id",
        default=os.environ.get("GITHUB_RUN_ID", ""),
        help="run id of the current cleanup execution",
    )
    parser.add_argument("--repo-root", default=".", help="repository root used to confine --config")
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

    try:
        keep_latest = int(str(args.keep_latest).strip())
        options = CleanupOptions(
            mode=args.mode,
            keep_latest=keep_latest,
            run_runs=not args.skip_runs,
            run_branches=not args.skip_branches,
            central_repository=args.central_repository,
            current_run_id=args.current_run_id,
        )
    except ValueError as exc:
        print(f"::error::invalid input: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    try:
        config: RepositoryList = load_repository_config(args.config, repo_root=args.repo_root)
    except RepositoryConfigError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return EXIT_CONFIG

    if config.errors:
        print("::error::the repository list contains invalid entries:", file=sys.stderr)
        for error in config.errors:
            print(f"  - {error}", file=sys.stderr)
        return EXIT_CONFIG
    for duplicate in config.duplicates:
        print(f"::notice::duplicate repository ignored: {duplicate}")
    if not config.repositories:
        print("::error::no repositories are configured in the repository list", file=sys.stderr)
        return EXIT_CONFIG

    try:
        selected = select_repositories(config.repositories, args.repository)
    except RepositoryConfigError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return EXIT_CONFIG

    log = lambda message: print(redact(message, token))  # noqa: E731
    repo_root = Path(args.repo_root).resolve()
    script_path = repo_root / "tool" / "branch_cleanup.py"
    if not args.skip_branches and not script_path.is_file():
        print(f"::error::branch cleanup script not found: {script_path}", file=sys.stderr)
        return EXIT_CONFIG

    client = GhCliClient(token=token)
    workspace_factory = GitWorkspaceFactory(
        token=token,
        base_dir=args.workspace_dir or None,
        log=log,
    )
    branch_runner = BranchCleanupRunner(
        token=token,
        script_path=script_path,
        log=log,
    )
    writer = ReportWriter(args.reports_dir, token=token, mode=options.mode_label)

    cleanup = CentralCleanup(
        client=client,
        workspace_factory=workspace_factory,
        branch_runner=branch_runner,
        writer=writer,
        options=options,
        raw_report_dir=Path(args.reports_dir) / "raw",
        log=log,
        token=token,
    )

    log(
        f"Central cleanup starting — mode={options.mode_label} keep_latest={options.keep_latest} "
        f"repositories={len(selected)} runs={'on' if options.run_runs else 'off'} "
        f"branches={'on' if options.run_branches else 'off'}"
    )
    reports = cleanup.run(selected)

    failed = [report for report in reports if report.status == "failed"]
    totals = aggregate(reports)
    log(
        "Central cleanup finished — "
        f"repositories={len(reports)} failed={len(failed)} "
        f"runs_deleted={totals['runs_deleted']} "
        f"runs_planned={totals['runs_planned_deletions']} "
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

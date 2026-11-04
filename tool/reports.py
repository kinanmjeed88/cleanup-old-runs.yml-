#!/usr/bin/env python3
"""Per-repository and aggregate reporting for the central cleanup.

Reports are written as JSON (machine readable) and Markdown (human readable),
and appended to ``$GITHUB_STEP_SUMMARY``. Every string passes through
:func:`tool.github_client.redact`, so a token can never reach a report.

Status vocabulary (per repository):

``DISCOVERED``        found by discovery, not processed yet
``DRY_RUN_SUCCESS``   preview completed; nothing was deleted
``LIVE_SUCCESS``      live execution completed without operation failures
``PARTIAL_FAILURE``   some mutations succeeded but at least one operation failed
``FAILED``            no successful mutation and at least one failure
``SKIPPED``           deliberately not modified (archived, no write indicator,
                      no access, or no enabled phase) — a reason is recorded
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from tool.github_client import redact

MAX_LISTED_ITEMS = 500

STATUS_DISCOVERED = "DISCOVERED"
STATUS_DRY_RUN = "DRY_RUN_SUCCESS"
STATUS_LIVE = "LIVE_SUCCESS"
STATUS_PARTIAL = "PARTIAL_FAILURE"
STATUS_SKIPPED = "SKIPPED"
STATUS_FAILED = "FAILED"
# Legacy label kept only so old imports do not crash; new code must not use it.
STATUS_ACCESSIBLE = "ACCESSIBLE"

ALL_STATUSES = (
    STATUS_DISCOVERED,
    STATUS_DRY_RUN,
    STATUS_LIVE,
    STATUS_PARTIAL,
    STATUS_SKIPPED,
    STATUS_FAILED,
)

UNSUCCESSFUL_STATUSES = frozenset({STATUS_FAILED, STATUS_PARTIAL})


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class BranchDecision:
    branch: str
    action: str
    reason: str

    def as_dict(self) -> dict:
        return {"branch": self.branch, "action": self.action, "reason": self.reason}


@dataclass
class RepoReport:
    repository: str
    mode: str
    started_at: str = field(default_factory=utc_now_iso)
    finished_at: str = ""
    status: str = STATUS_DISCOVERED

    # discovery metadata
    owner_login: str = ""
    owner_type: str = ""
    visibility: str = ""
    default_branch: Optional[str] = None
    archived: bool = False
    fork: bool = False
    discovered_from: list[str] = field(default_factory=list)
    discovery_status: str = "discovered"
    exclusion_reason: str = ""

    # Counter contract (kept identical in JSON, Markdown and the step summary):
    # retained = size of the keep plan, not a deletion count; deleted =
    # successful DELETE responses only; already_absent = run-specific DELETE
    # 404 responses, checked again by verification; remaining = observed count
    # in dry-run, or the post-execution re-list count after live attempts.
    runs_examined: int = 0
    runs_completed: int = 0
    runs_active: int = 0
    runs_unverifiable: int = 0
    runs_retained: int = 0
    runs_deleted: int = 0
    runs_already_absent: int = 0
    runs_planned_deletions: int = 0
    runs_excluded: int = 0
    runs_failed: int = 0
    runs_remaining: Optional[int] = None

    branches_examined: int = 0
    branches_kept: int = 0
    branches_deleted: int = 0
    branches_planned_deletions: int = 0
    branches_failed: int = 0

    keep_latest: int = 0
    permissions: list[dict] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    branch_decisions: list[BranchDecision] = field(default_factory=list)
    excluded_runs: list[dict] = field(default_factory=list)
    retention: dict = field(default_factory=dict)
    verification: dict = field(default_factory=dict)
    branch_listing: dict = field(default_factory=dict)

    @property
    def operation_failures(self) -> int:
        return self.runs_failed + self.branches_failed

    @property
    def failure_count(self) -> int:
        # Operation failures are already materialised as entries in
        # ``failures``; counting them again here double-counted every bad run.
        return len(self.failures)

    @property
    def live(self) -> bool:
        return self.mode.upper() == "LIVE"

    @property
    def can_write(self) -> bool:
        return self._permission_ok("contents", "write")

    @property
    def actions_read(self) -> bool:
        return self._permission_ok("actions", "read")

    @property
    def actions_write_status(self) -> str:
        for check in self.permissions:
            if check.get("scope") == "actions" and check.get("access") == "write":
                return str(check.get("status") or "unverified")
        return "unverified"

    def _permission_ok(self, scope: str, access: str) -> bool:
        return any(
            check.get("scope") == scope
            and check.get("access") == access
            and check.get("status") == "ok"
            for check in self.permissions
        )

    def add_failure(self, message: str) -> None:
        if len(self.failures) < MAX_LISTED_ITEMS:
            self.failures.append(message)
        self.status = STATUS_FAILED

    def add_note(self, message: str) -> None:
        if len(self.notes) < MAX_LISTED_ITEMS:
            self.notes.append(message)

    def skip(self, reason: str) -> None:
        """Mark the repository as skipped, keeping the reason explicit."""
        self.exclusion_reason = reason
        if self.status not in UNSUCCESSFUL_STATUSES:
            self.status = STATUS_SKIPPED
        self.add_note(f"skipped: {reason}")

    def finalise_status(self, *, live: bool, executed_any_phase: bool) -> None:
        """Apply the required success/partial/failed/skipped vocabulary."""
        if self.failures or self.operation_failures:
            # A DELETE 404 is only an idempotent success once the re-list has
            # verified absence.  If verification failed/mismatched, it must not
            # turn an otherwise failed repository into PARTIAL_FAILURE.
            confirmed_already_absent = (
                self.runs_already_absent > 0
                and self.verification.get("status") == "verified"
            )
            if self.runs_deleted or self.branches_deleted or confirmed_already_absent:
                self.status = STATUS_PARTIAL
            else:
                self.status = STATUS_FAILED
            return
        if self.status == STATUS_SKIPPED:
            return
        if not executed_any_phase:
            self.skip("no cleanup phase was enabled or permitted for this repository")
            return
        self.status = STATUS_LIVE if live else STATUS_DRY_RUN

    def as_dict(self) -> dict:
        return {
            "repository": self.repository,
            "mode": self.mode,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "owner_login": self.owner_login,
            "owner_type": self.owner_type,
            "visibility": self.visibility,
            "default_branch": self.default_branch,
            "archived": self.archived,
            "fork": self.fork,
            "discovered_from": list(self.discovered_from),
            "discovery_status": self.discovery_status,
            "exclusion_reason": self.exclusion_reason,
            "can_write": self.can_write,
            "actions_read": self.actions_read,
            "actions_write_status": self.actions_write_status,
            "failure_count": self.failure_count,
            "counts": {
                "runs_examined": self.runs_examined,
                "runs_completed": self.runs_completed,
                "runs_active": self.runs_active,
                "runs_unverifiable": self.runs_unverifiable,
                "runs_retained": self.runs_retained,
                "runs_deleted": self.runs_deleted,
                "runs_already_absent": self.runs_already_absent,
                "runs_planned_deletions": self.runs_planned_deletions,
                "runs_excluded": self.runs_excluded,
                "runs_failed": self.runs_failed,
                "runs_remaining": self.runs_remaining,
                "branches_examined": self.branches_examined,
                "branches_kept": self.branches_kept,
                "branches_deleted": self.branches_deleted,
                "branches_planned_deletions": self.branches_planned_deletions,
                "branches_failed": self.branches_failed,
            },
            "keep_latest": self.keep_latest,
            "permissions": list(self.permissions),
            "failures": list(self.failures),
            "notes": list(self.notes),
            "branch_decisions": [decision.as_dict() for decision in self.branch_decisions],
            "excluded_runs": list(self.excluded_runs),
            "retention": dict(self.retention),
            "verification": dict(self.verification),
            "branch_listing": dict(self.branch_listing),
        }


def aggregate(reports: Iterable[RepoReport]) -> dict:
    reports = list(reports)
    by_status = {status: 0 for status in ALL_STATUSES}
    for report in reports:
        by_status[report.status] = by_status.get(report.status, 0) + 1
    failed = by_status.get(STATUS_FAILED, 0)
    partial = by_status.get(STATUS_PARTIAL, 0)
    return {
        "repositories": len(reports),
        "by_status": by_status,
        "repositories_discovered": len(reports),
        "repositories_failed": failed,
        "repositories_partial": partial,
        "repositories_unsuccessful": failed + partial,
        "repositories_skipped": by_status.get(STATUS_SKIPPED, 0),
        "repositories_dry_run": by_status.get(STATUS_DRY_RUN, 0),
        "repositories_live": by_status.get(STATUS_LIVE, 0),
        "repositories_ok": sum(
            1 for report in reports if report.status in (STATUS_DRY_RUN, STATUS_LIVE)
        ),
        "runs_examined": sum(r.runs_examined for r in reports),
        "runs_completed": sum(r.runs_completed for r in reports),
        "runs_active": sum(r.runs_active for r in reports),
        "runs_retained": sum(r.runs_retained for r in reports),
        "runs_deleted": sum(r.runs_deleted for r in reports),
        "runs_already_absent": sum(r.runs_already_absent for r in reports),
        "runs_planned_deletions": sum(r.runs_planned_deletions for r in reports),
        "runs_excluded": sum(r.runs_excluded for r in reports),
        "runs_failed": sum(r.runs_failed for r in reports),
        "branches_examined": sum(r.branches_examined for r in reports),
        "branches_kept": sum(r.branches_kept for r in reports),
        "branches_deleted": sum(r.branches_deleted for r in reports),
        "branches_planned_deletions": sum(r.branches_planned_deletions for r in reports),
        "branches_failed": sum(r.branches_failed for r in reports),
        "failures": sum(r.failure_count for r in reports),
    }


def _safe_slug(repository: str) -> str:
    return repository.replace("/", "__").replace("..", "_")


def render_discovery_table(discovery: Optional[dict]) -> list[str]:
    """Markdown section listing every discovered repository."""
    if not discovery:
        return []
    lines = [
        "## Discovered repositories",
        "",
        f"- Authenticated as: `{discovery.get('authenticated_user') or 'unknown'}`",
        f"- Repositories discovered: **{discovery.get('count', 0)}**",
        f"- Discovery complete: {'yes' if discovery.get('complete') else 'NO (see errors)'}",
        "",
    ]
    sources = discovery.get("sources", [])
    if sources:
        lines += [
            "| Source | Kind | Repositories | Pages | Complete |",
            "|---|---|---:|---:|---|",
        ]
        for source in sources:
            lines.append(
                f"| `{source.get('endpoint')}` | {source.get('kind')} | {source.get('repositories')} "
                f"| {source.get('pages_read')} | {'yes' if source.get('complete') else 'NO'} |"
            )
        lines.append("")
    repositories = discovery.get("repositories", [])
    if repositories:
        lines += [
            "| Repository | Owner | Type | Visibility | Default | Archived | Fork | Can write | Sources |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for repo in repositories:
            lines.append(
                f"| `{repo['full_name']}` | {repo['owner_login']} | {repo['owner_type']} "
                f"| {repo['visibility']} | `{repo['default_branch']}` "
                f"| {'yes' if repo['archived'] else 'no'} | {'yes' if repo['fork'] else 'no'} "
                f"| {'yes' if repo['can_write'] else 'no'} | {', '.join(repo['sources'])} |"
            )
        lines.append("")
    for note in discovery.get("notes", []):
        lines.append(f"> {note}")
    for error in discovery.get("errors", []):
        lines.append(f"> **discovery error:** {error}")
    if discovery.get("notes") or discovery.get("errors"):
        lines.append("")
    return lines


def _counter_definitions() -> list[str]:
    """Shared legend so Markdown cannot drift from the JSON field meanings."""
    return [
        "",
        "### Counter definitions",
        "",
        "- Retained (`runs_retained`): runs in the keep plan — normally the newest eligible "
        "completed runs; on an aborted/incomplete listing this is the conservative keep-all set. "
        "It is not a deletion count.",
        "- Planned deletions (`runs_planned_deletions`): candidates selected by the retention plan.",
        "- Deleted (`runs_deleted`): successful DELETE responses only (HTTP 200/204).",
        "- Already absent (`runs_already_absent`): run-specific DELETE 404 responses **only after** "
        "a complete post-execution re-list confirms the run id is gone. Raw 404 responses are kept "
        "separately as `retention.runs_delete_404` and are not counted as successes.",
        "- Failed (`runs_failed`): failed DELETE attempts, including HTTP 422. Continuing after "
        "a 422 never clears the failure.",
        "- Remaining (`runs_remaining`): the observed listing count in dry-run (nothing was "
        "deleted), or the complete post-execution re-list count after live deletion attempts; "
        "`unknown`/`null` when discovery or verification is incomplete, or a live execution "
        "aborted before a trustworthy re-list.",
        "- Verification: checks that resolved ids are absent and protected ids remain. Failed "
        "and unattempted candidates are expected survivors, not successes; operation failures "
        "still make the repository PARTIAL_FAILURE or FAILED.",
    ]


def _runs_line(report: RepoReport) -> str:
    remaining = "unknown" if report.runs_remaining is None else str(report.runs_remaining)
    verification = report.verification.get("status") or "not_attempted"
    return (
        f"- Runs: examined {report.runs_examined}, completed {report.runs_completed}, "
        f"active {report.runs_active}, unverifiable {report.runs_unverifiable}, "
        f"retained {report.runs_retained}, planned deletions {report.runs_planned_deletions}, "
        f"deleted {report.runs_deleted}, already absent {report.runs_already_absent}, "
        f"excluded {report.runs_excluded}, failed {report.runs_failed}, "
        f"remaining {remaining}, verification {verification}"
    )


def render_markdown(
    reports: list[RepoReport],
    *,
    mode: str,
    generated_at: str,
    totals: dict,
    discovery: Optional[dict] = None,
) -> str:
    lines = [
        f"# Central cleanup report — {mode.upper()}",
        "",
        f"- Generated: `{generated_at}`",
        f"- Repositories: {totals['repositories']} "
        f"(dry-run success: {totals['repositories_dry_run']}, live success: {totals['repositories_live']}, "
        f"partial: {totals['repositories_partial']}, failed: {totals['repositories_failed']}, "
        f"skipped: {totals['repositories_skipped']})",
        "",
    ]
    if discovery:
        lines.insert(5, f"- Repositories discovered: **{discovery.get('count', 0)}**")
    lines += [
        "## Summary",
        "",
        "| Repository | Status | Runs scanned | Completed | Active | Retained | Deleted | Remaining | Verification | Failures |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|---:|",
    ]
    for report in reports:
        remaining = "unknown" if report.runs_remaining is None else str(report.runs_remaining)
        verification = report.verification.get("status") or "not_attempted"
        lines.append(
            f"| `{report.repository}` | {report.status} | {report.runs_examined} "
            f"| {report.runs_completed} | {report.runs_active} | {report.runs_retained} "
            f"| {report.runs_deleted} | {remaining} | {verification} | {report.failure_count} |"
        )
    lines += [
        f"| **Total** | | {totals['runs_examined']} | {totals['runs_completed']} "
        f"| {totals['runs_active']} | {totals['runs_retained']} | {totals['runs_deleted']} "
        f"| | | {totals['failures']} |",
        "",
    ]
    lines += _counter_definitions()
    lines.append("")
    lines += render_discovery_table(discovery)
    lines += ["## Details", ""]
    for report in reports:
        lines.append(f"### `{report.repository}` — {report.status} ({report.mode})")
        lines.append("")
        lines.append(
            f"- Owner: {report.owner_login or 'unknown'} ({report.owner_type or 'unknown'}), "
            f"{report.visibility or 'unknown'}"
            + (", archived" if report.archived else "")
            + (", fork" if report.fork else "")
        )
        lines.append(f"- Default branch: `{report.default_branch or 'unknown'}`")
        lines.append(f"- Discovery: {report.discovery_status} from {', '.join(report.discovered_from) or 'n/a'}")
        if report.exclusion_reason:
            lines.append(f"- Exclusion reason: {report.exclusion_reason}")
        lines.append(f"- Retention: newest {report.keep_latest} completed workflow runs")
        lines.append(
            f"- Permissions: contents write {'ok' if report.can_write else 'missing/unverified'}, "
            f"actions read {'ok' if report.actions_read else 'missing'}, "
            f"actions write {report.actions_write_status}"
        )
        lines.append(_runs_line(report))
        lines.append(
            f"- Branches: examined {report.branches_examined}, kept {report.branches_kept}, "
            f"planned deletions {report.branches_planned_deletions}, deleted {report.branches_deleted}, "
            f"failed {report.branches_failed}"
        )
        if report.verification:
            lines.append(f"- Post-execution verification: `{json.dumps(report.verification, ensure_ascii=False)}`")
        if report.notes:
            lines.append("- Notes:")
            lines.extend(f"  - {note}" for note in report.notes)
        if report.failures:
            lines.append("- Failures:")
            lines.extend(f"  - {failure}" for failure in report.failures)
        if report.branch_decisions:
            lines.append("")
            lines.append("| Branch | Action | Reason |")
            lines.append("|---|---|---|")
            for decision in report.branch_decisions[:MAX_LISTED_ITEMS]:
                reason = decision.reason.replace("|", "\\|")
                lines.append(f"| `{decision.branch}` | {decision.action} | {reason} |")
            if len(report.branch_decisions) > MAX_LISTED_ITEMS:
                lines.append(f"| … | … | {len(report.branch_decisions) - MAX_LISTED_ITEMS} more not shown |")
        lines.append("")
    return "\n".join(lines)


def render_step_summary(
    reports: list[RepoReport],
    *,
    mode: str,
    totals: dict,
    discovery: Optional[dict] = None,
) -> str:
    lines = [
        f"## Central cleanup — {mode.upper()}",
        "",
        f"Discovered: {discovery.get('count', 0) if discovery else 'n/a'} | "
        f"Processed: {totals['repositories']} | Failed: {totals['repositories_failed']} | "
        f"Partial: {totals['repositories_partial']} | Skipped: {totals['repositories_skipped']} | "
        f"Runs deleted: {totals['runs_deleted']} | Branches deleted: {totals['branches_deleted']}",
        "",
        "| Repository | Status | Runs scanned | Retained | Deleted | Remaining | Verification | Failures |",
        "|---|---|---:|---:|---:|---:|---|---:|",
    ]
    for report in reports:
        remaining = "unknown" if report.runs_remaining is None else str(report.runs_remaining)
        verification = report.verification.get("status") or "not_attempted"
        lines.append(
            f"| `{report.repository}` | {report.status} | {report.runs_examined} | {report.runs_retained} "
            f"| {report.runs_deleted} | {remaining} | {verification} | {report.failure_count} |"
        )
    lines += _counter_definitions()
    failing = [report for report in reports if report.failures]
    if failing:
        lines += ["", "### Failures", ""]
        for report in failing:
            for failure in report.failures[:10]:
                lines.append(f"- `{report.repository}`: {failure}")
    skipped = [report for report in reports if report.status == STATUS_SKIPPED]
    if skipped:
        lines += ["", "### Skipped", ""]
        for report in skipped:
            lines.append(f"- `{report.repository}`: {report.exclusion_reason or 'excluded'}")
    return "\n".join(lines) + "\n"


class ReportWriter:
    """Writes reports to disk and to the GitHub step summary."""

    def __init__(self, output_dir: str | Path, *, token: str = "", mode: str = "dry-run") -> None:
        self.output_dir = Path(output_dir).resolve()
        self.repo_dir = self.output_dir / "reports"
        self.token = token
        self.mode = mode

    def _write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(redact(content, self.token), encoding="utf-8")

    def write_repository(self, report: RepoReport) -> dict[str, Path]:
        slug = _safe_slug(report.repository)
        json_path = self.repo_dir / f"{slug}.json"
        md_path = self.repo_dir / f"{slug}.md"
        report.finished_at = report.finished_at or utc_now_iso()

        self._write(json_path, json.dumps(report.as_dict(), indent=2, ensure_ascii=False) + "\n")
        markdown = [
            f"# Cleanup report — `{report.repository}`",
            "",
            f"- Status: **{report.status}**",
            f"- Mode: **{report.mode.upper()}**",
            f"- Started: `{report.started_at}`",
            f"- Finished: `{report.finished_at}`",
            f"- Owner: `{report.owner_login or 'unknown'}` ({report.owner_type or 'unknown'})",
            f"- Visibility: {report.visibility or 'unknown'}"
            + (", archived" if report.archived else "")
            + (", fork" if report.fork else ""),
            f"- Discovered from: {', '.join(report.discovered_from) or 'n/a'}",
            f"- Default branch: `{report.default_branch or 'unknown'}`",
            f"- Contents write: {'yes' if report.can_write else 'no'}",
            f"- Actions read: {'yes' if report.actions_read else 'no'}",
            f"- Actions write: {report.actions_write_status}",
        ]
        if report.exclusion_reason:
            markdown.append(f"- Exclusion reason: {report.exclusion_reason}")
        markdown += [
            "",
            "## Workflow runs",
            "",
            f"- Examined: {report.runs_examined}",
            f"- Completed: {report.runs_completed}",
            f"- Active: {report.runs_active}",
            f"- Unverifiable: {report.runs_unverifiable}",
            f"- Retained (newest {report.keep_latest} completed): {report.runs_retained}",
            f"- Planned deletions: {report.runs_planned_deletions}",
            f"- Deleted: {report.runs_deleted}",
            f"- Already absent (idempotent): {report.runs_already_absent}",
            f"- Excluded: {report.runs_excluded}",
            f"- Failed: {report.runs_failed}",
            f"- Remaining: {'unknown' if report.runs_remaining is None else report.runs_remaining}",
            f"- Verification: {report.verification.get('status') or 'not_attempted'}",
        ]
        markdown += _counter_definitions()
        markdown += [
            "",
            "## Branches",
            "",
            f"- Examined: {report.branches_examined}",
            f"- Kept: {report.branches_kept}",
            f"- Planned deletions: {report.branches_planned_deletions}",
            f"- Deleted: {report.branches_deleted}",
            f"- Failed: {report.branches_failed}",
        ]
        if report.branch_listing:
            listing = report.branch_listing
            markdown += [
                "",
                "### Branch listing diagnostics",
                "",
                f"- Source: `{listing.get('source', 'unknown')}`",
                f"- Complete: {listing.get('complete')}",
                f"- Empty verified: {listing.get('empty_verified')}",
                f"- Branch count: {listing.get('branch_count')}",
                f"- Pages read: {listing.get('pages_read')}",
                f"- HTTP status: {listing.get('http_status')}",
                f"- Exit code: {listing.get('returncode')}",
            ]
            if listing.get("error"):
                markdown.append(f"- Error: {listing['error']}")
            if listing.get("stderr"):
                markdown.append(f"- stderr: `{listing['stderr']}`")
            for warning in listing.get("warnings", []) or []:
                markdown.append(f"- Warning: {warning}")
        if report.branch_decisions:
            markdown += ["", "### Branch decisions", "", "| Branch | Action | Reason |", "|---|---|---|"]
            for decision in report.branch_decisions[:MAX_LISTED_ITEMS]:
                reason = decision.reason.replace("|", "\\|")
                markdown.append(f"| `{decision.branch}` | {decision.action} | {reason} |")
        if report.failures:
            markdown += ["", "### Failures", ""]
            markdown += [f"- {failure}" for failure in report.failures]
        if report.notes:
            markdown += ["", "### Notes", ""]
            markdown += [f"- {note}" for note in report.notes]
        self._write(md_path, "\n".join(markdown) + "\n")
        return {"json": json_path, "markdown": md_path}

    def write_aggregate(
        self, reports: list[RepoReport], *, discovery: Optional[dict] = None
    ) -> dict[str, Any]:
        reports = list(reports)
        totals = aggregate(reports)
        generated_at = utc_now_iso()
        payload = {
            "mode": self.mode,
            "generated_at": generated_at,
            "totals": totals,
            "discovery": discovery,
            "repositories": [report.as_dict() for report in reports],
        }
        json_path = self.output_dir / "summary.json"
        md_path = self.output_dir / "REPORT.md"
        self._write(json_path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        self._write(
            md_path,
            render_markdown(
                reports, mode=self.mode, generated_at=generated_at, totals=totals, discovery=discovery
            ),
        )
        return {"json": json_path, "markdown": md_path, "totals": totals}

    def append_step_summary(
        self, reports: list[RepoReport], *, discovery: Optional[dict] = None
    ) -> None:
        destination = os.environ.get("GITHUB_STEP_SUMMARY", "")
        if not destination:
            return
        totals = aggregate(reports)
        try:
            with open(destination, "a", encoding="utf-8") as handle:
                handle.write(
                    redact(
                        render_step_summary(
                            reports, mode=self.mode, totals=totals, discovery=discovery
                        ),
                        self.token,
                    )
                )
        except OSError:  # pragma: no cover - environment specific
            pass

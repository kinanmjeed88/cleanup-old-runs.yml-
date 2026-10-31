#!/usr/bin/env python3
"""Per-repository and aggregate reporting for the central cleanup.

Reports are written as JSON (machine readable) and Markdown (human readable) and
appended to ``$GITHUB_STEP_SUMMARY``. Every string is passed through
:func:`tool.github_client.redact` so a token can never reach a report.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from tool.github_client import redact

MAX_LISTED_ITEMS = 500


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
    status: str = "ok"  # ok | warning | failed
    default_branch: Optional[str] = None

    runs_examined: int = 0
    runs_retained: int = 0
    runs_deleted: int = 0
    runs_planned_deletions: int = 0
    runs_excluded: int = 0
    runs_failed: int = 0

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

    @property
    def operation_failures(self) -> int:
        return self.runs_failed + self.branches_failed

    @property
    def failure_count(self) -> int:
        return len(self.failures) + self.operation_failures

    @property
    def live(self) -> bool:
        return self.mode.lower() == "live"

    def add_failure(self, message: str) -> None:
        if len(self.failures) < MAX_LISTED_ITEMS:
            self.failures.append(message)
        if self.status == "ok":
            self.status = "failed"

    def add_note(self, message: str) -> None:
        if len(self.notes) < MAX_LISTED_ITEMS:
            self.notes.append(message)

    def as_dict(self) -> dict:
        payload = {
            "repository": self.repository,
            "mode": self.mode,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "default_branch": self.default_branch,
            "failure_count": self.failure_count,
            "counts": {
                "runs_examined": self.runs_examined,
                "runs_retained": self.runs_retained,
                "runs_deleted": self.runs_deleted,
                "runs_planned_deletions": self.runs_planned_deletions,
                "runs_excluded": self.runs_excluded,
                "runs_failed": self.runs_failed,
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
        }
        return payload


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def aggregate(reports: Iterable[RepoReport]) -> dict:
    reports = list(reports)
    totals = {
        "repositories": len(reports),
        "repositories_ok": sum(1 for r in reports if r.status == "ok"),
        "repositories_warning": sum(1 for r in reports if r.status == "warning"),
        "repositories_failed": sum(1 for r in reports if r.status == "failed"),
        "runs_examined": sum(r.runs_examined for r in reports),
        "runs_retained": sum(r.runs_retained for r in reports),
        "runs_deleted": sum(r.runs_deleted for r in reports),
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
    return totals


def _safe_slug(repository: str) -> str:
    return repository.replace("/", "__").replace("..", "_")


def render_markdown(reports: list[RepoReport], *, mode: str, generated_at: str, totals: dict) -> str:
    lines = [
        "# Central cleanup report",
        "",
        f"- Mode: **{mode.upper()}**",
        f"- Generated at: `{generated_at}`",
        f"- Repositories processed: {totals['repositories']} "
        f"(ok: {totals['repositories_ok']}, warning: {totals['repositories_warning']}, "
        f"failed: {totals['repositories_failed']})",
        "",
        "## Summary",
        "",
        "| Repository | Status | Runs scanned | Runs retained | Runs deleted | Runs excluded "
        "| Branches scanned | Branches kept | Branches deleted | Failures |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for report in reports:
        lines.append(
            f"| `{report.repository}` | {report.status} | {report.runs_examined} | {report.runs_retained} "
            f"| {report.runs_deleted} | {report.runs_excluded} | {report.branches_examined} "
            f"| {report.branches_kept} | {report.branches_deleted} | {report.failure_count} |"
        )
    lines += [
        f"| **Total** | | {totals['runs_examined']} | {totals['runs_retained']} | {totals['runs_deleted']} "
        f"| {totals['runs_excluded']} | {totals['branches_examined']} | {totals['branches_kept']} "
        f"| {totals['branches_deleted']} | {totals['failures']} |",
        "",
        "## Details",
        "",
    ]
    for report in reports:
        lines.append(f"### `{report.repository}` — {report.status} ({report.mode})")
        lines.append("")
        lines.append(f"- Default branch: `{report.default_branch or 'unknown'}`")
        lines.append(f"- Retention: newest {report.keep_latest} completed workflow runs")
        lines.append(
            f"- Runs: examined {report.runs_examined}, retained {report.runs_retained}, "
            f"deleted {report.runs_deleted}, excluded {report.runs_excluded}, failed {report.runs_failed}"
        )
        lines.append(
            f"- Branches: examined {report.branches_examined}, kept {report.branches_kept}, "
            f"deleted {report.branches_deleted}, failed {report.branches_failed}"
        )
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


def render_step_summary(reports: list[RepoReport], *, mode: str, totals: dict) -> str:
    lines = [
        f"## Central cleanup — {mode.upper()}",
        "",
        f"Repositories: {totals['repositories']} | Failed: {totals['repositories_failed']} "
        f"| Runs deleted: {totals['runs_deleted']} | Branch deletions: {totals['branches_deleted']} "
        f"| Failures: {totals['failures']}",
        "",
        "| Repository | Status | Runs scanned | Retained | Deleted | Excluded | Branches | Kept | Deleted | Failures |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for report in reports:
        lines.append(
            f"| `{report.repository}` | {report.status} | {report.runs_examined} | {report.runs_retained} "
            f"| {report.runs_deleted} | {report.runs_excluded} | {report.branches_examined} "
            f"| {report.branches_kept} | {report.branches_deleted} | {report.failure_count} |"
        )
    failing = [report for report in reports if report.failures]
    if failing:
        lines += ["", "### Failures", ""]
        for report in failing:
            for failure in report.failures[:10]:
                lines.append(f"- `{report.repository}`: {failure}")
    return "\n".join(lines) + "\n"


class ReportWriter:
    """Writes reports to disk and to the GitHub step summary."""

    def __init__(self, output_dir: str | Path, *, token: str = "", mode: str = "dry-run") -> None:
        self.output_dir = Path(output_dir)
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
            f"- Mode: **{report.mode.upper()}**",
            f"- Status: **{report.status}**",
            f"- Started: `{report.started_at}`",
            f"- Finished: `{report.finished_at}`",
            f"- Default branch: `{report.default_branch or 'unknown'}`",
            "",
            "## Workflow runs",
            "",
            f"- Examined: {report.runs_examined}",
            f"- Retained (newest {report.keep_latest} completed): {report.runs_retained}",
            f"- Deleted: {report.runs_deleted}",
            f"- Planned deletions: {report.runs_planned_deletions}",
            f"- Excluded: {report.runs_excluded}",
            f"- Failed: {report.runs_failed}",
            "",
            "## Branches",
            "",
            f"- Examined: {report.branches_examined}",
            f"- Kept: {report.branches_kept}",
            f"- Deleted: {report.branches_deleted}",
            f"- Planned deletions: {report.branches_planned_deletions}",
            f"- Failed: {report.branches_failed}",
        ]
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

    def write_aggregate(self, reports: list[RepoReport]) -> dict[str, Path]:
        reports = list(reports)
        totals = aggregate(reports)
        generated_at = utc_now_iso()
        payload = {
            "mode": self.mode,
            "generated_at": generated_at,
            "totals": totals,
            "repositories": [report.as_dict() for report in reports],
        }
        json_path = self.output_dir / "summary.json"
        md_path = self.output_dir / "REPORT.md"
        self._write(json_path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        self._write(md_path, render_markdown(reports, mode=self.mode, generated_at=generated_at, totals=totals))
        return {"json": json_path, "markdown": md_path, "totals": totals}

    def append_step_summary(self, reports: list[RepoReport]) -> None:
        destination = os.environ.get("GITHUB_STEP_SUMMARY", "")
        if not destination:
            return
        totals = aggregate(reports)
        try:
            with open(destination, "a", encoding="utf-8") as handle:
                handle.write(redact(render_step_summary(reports, mode=self.mode, totals=totals), self.token))
        except OSError:  # pragma: no cover - environment specific
            pass


def report_to_dict(report: RepoReport) -> dict:
    """Convenience wrapper used by tests."""
    return asdict(report)

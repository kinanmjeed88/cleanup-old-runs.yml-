#!/usr/bin/env python3
"""Retention policy: keep the newest N *completed* workflow runs per repository.

The policy is deliberately pure: :func:`plan_retention` takes the runs that were
read from the API (plus a flag telling whether that listing is complete) and
returns which runs may be deleted. It performs no I/O, so every rule below is
unit-testable without a network.

Safety rules (all fail *closed* — an unsure case keeps the run):

1. The run executing the current cleanup is never deleted.
2. Runs that are not ``completed`` (queued / in progress / waiting / pending /
   requested) are never deleted.
3. Runs whose status, id or ``created_at`` cannot be parsed are never deleted,
   because they cannot be ordered reliably.
4. If the listing could not be completed (a page failed, the reported
   ``total_count`` was not reached, or an entry was malformed), nothing is
   deleted for that repository.
5. Ordering is per repository: ``keep_latest`` is never applied to a pool that
   mixes repositories.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional, Sequence

DEFAULT_KEEP_LATEST = 20
MIN_KEEP_LATEST = 1
MAX_KEEP_LATEST = 10_000

PER_PAGE = 100
MAX_PAGES = 200  # 20 000 runs; exceeding this aborts instead of guessing

ACTIVE_STATUSES = frozenset({"queued", "in_progress", "waiting", "pending", "requested"})
COMPLETED_STATUS = "completed"
KNOWN_STATUSES = ACTIVE_STATUSES | {COMPLETED_STATUS}

# Stable exclusion reasons (asserted by tests and shown in reports).
REASON_CURRENT_RUN = "current cleanup run"
REASON_DUPLICATE = "duplicate run id in listing"
REASON_UNVERIFIABLE_ID = "unverifiable run id"
REASON_UNVERIFIABLE_STATUS = "unverifiable status"
REASON_UNVERIFIABLE_CREATED_AT = "unverifiable created_at"
REASON_OLDER_THAN_RETENTION = "older than the newest {keep} completed runs"


@dataclass(frozen=True)
class WorkflowRun:
    """One entry of ``GET /repos/{owner}/{repo}/actions/runs``."""

    id: Optional[int]
    created_at: str = ""
    status: str = ""
    name: str = ""
    workflow_id: Optional[int] = None
    event: str = ""

    @property
    def created_dt(self) -> Optional[datetime]:
        return parse_timestamp(self.created_at)

    @classmethod
    def from_api(cls, payload: Any) -> "WorkflowRun":
        if not isinstance(payload, dict):
            return cls(None, "", "", "")
        return cls(
            id=_coerce_int(payload.get("id")),
            created_at=payload.get("created_at") or "",
            status=payload.get("status") or "",
            name=payload.get("name") or "",
            workflow_id=_coerce_int(payload.get("workflow_id")),
            event=payload.get("event") or "",
        )

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "status": self.status,
            "name": self.name,
            "workflow_id": self.workflow_id,
            "event": self.event,
        }


def _coerce_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        try:
            return int(value)
        except ValueError:  # pragma: no cover - defensive only
            return None
    return None


def parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse a GitHub timestamp into an aware UTC datetime, or ``None``."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z") or raw.endswith("z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class ExcludedRun:
    run: WorkflowRun
    reason: str

    def as_dict(self) -> dict:
        return {"id": self.run.id, "created_at": self.run.created_at, "reason": self.reason}


@dataclass
class RetentionPlan:
    repository: str
    keep_latest: int
    keep: list[WorkflowRun] = field(default_factory=list)
    delete: list[WorkflowRun] = field(default_factory=list)
    excluded: list[ExcludedRun] = field(default_factory=list)
    examined: int = 0
    complete: bool = True
    errors: list[str] = field(default_factory=list)
    abort_reason: Optional[str] = None

    @property
    def aborted(self) -> bool:
        return self.abort_reason is not None

    def as_dict(self) -> dict:
        return {
            "repository": self.repository,
            "keep_latest": self.keep_latest,
            "examined": self.examined,
            "retained": len(self.keep),
            "deletion_candidates": len(self.delete),
            "excluded": len(self.excluded),
            "complete": self.complete,
            "aborted": self.aborted,
            "abort_reason": self.abort_reason,
            "errors": list(self.errors),
            "delete_ids": [run.id for run in self.delete],
            "excluded_runs": [item.as_dict() for item in self.excluded],
        }


@dataclass
class RunListing:
    """Outcome of paginating the runs endpoint."""

    runs: list[WorkflowRun] = field(default_factory=list)
    complete: bool = True
    errors: list[str] = field(default_factory=list)
    pages_read: int = 0
    reported_total: Optional[int] = None

    def as_dict(self) -> dict:
        return {
            "runs": len(self.runs),
            "pages_read": self.pages_read,
            "reported_total": self.reported_total,
            "complete": self.complete,
            "errors": list(self.errors),
        }


def validate_keep_latest(keep_latest: Any) -> int:
    """Validate the retention count, raising ``ValueError`` when unusable."""
    if isinstance(keep_latest, bool) or not isinstance(keep_latest, int):
        raise ValueError("keep_latest must be an integer")
    if keep_latest < MIN_KEEP_LATEST or keep_latest > MAX_KEEP_LATEST:
        raise ValueError(f"keep_latest must be between {MIN_KEEP_LATEST} and {MAX_KEEP_LATEST}")
    return keep_latest


def plan_retention(
    runs: Sequence[WorkflowRun],
    *,
    repository: str,
    keep_latest: int = DEFAULT_KEEP_LATEST,
    current_run_id: Optional[str] = None,
    complete: bool = True,
    errors: Optional[Iterable[str]] = None,
) -> RetentionPlan:
    """Decide which runs are retained and which may be deleted.

    ``complete`` must be False when the caller could not enumerate every run;
    in that case the plan deletes nothing.
    """
    plan = RetentionPlan(repository=repository, keep_latest=keep_latest)
    plan.examined = len(runs)
    plan.errors = list(errors or [])
    plan.complete = bool(complete)

    try:
        validate_keep_latest(keep_latest)
    except ValueError as exc:
        plan.abort_reason = f"invalid retention setting: {exc}"
        plan.keep = list(runs)
        return plan

    if not complete or plan.errors:
        plan.abort_reason = (
            "workflow run listing is incomplete; deletion skipped to avoid acting on a partial view"
        )
        plan.keep = list(runs)
        return plan

    current = str(current_run_id) if current_run_id not in (None, "") else None

    seen_ids: set[int] = set()
    ordered: list[tuple[datetime, int, WorkflowRun]] = []
    for run in runs:
        if run.id is None:
            plan.excluded.append(ExcludedRun(run, REASON_UNVERIFIABLE_ID))
            continue
        if current is not None and str(run.id) == current:
            plan.excluded.append(ExcludedRun(run, REASON_CURRENT_RUN))
            continue
        if run.status not in KNOWN_STATUSES:
            plan.excluded.append(ExcludedRun(run, REASON_UNVERIFIABLE_STATUS))
            continue
        if run.status != COMPLETED_STATUS:
            plan.excluded.append(ExcludedRun(run, f"active: {run.status}"))
            continue
        created = run.created_dt
        if created is None:
            plan.excluded.append(ExcludedRun(run, REASON_UNVERIFIABLE_CREATED_AT))
            continue
        if run.id in seen_ids:
            plan.excluded.append(ExcludedRun(run, REASON_DUPLICATE))
            continue
        seen_ids.add(run.id)
        ordered.append((created, run.id, run))

    # Newest first; tie-break on the (monotonic) run id so ordering is total.
    ordered.sort(key=lambda item: (item[0], item[1]), reverse=True)

    plan.keep = [item[2] for item in ordered[:keep_latest]]
    plan.delete = [item[2] for item in ordered[keep_latest:]]
    return plan


def fetch_all_runs(
    client: Any,
    repository: str,
    *,
    per_page: int = PER_PAGE,
    max_pages: int = MAX_PAGES,
) -> RunListing:
    """Paginate ``actions/runs`` until the listing is exhausted.

    Any failure marks the listing incomplete; the caller must then skip
    deletion for this repository. No assumption is made that the first page
    holds every run.
    """
    listing = RunListing()
    page = 1
    while page <= max_pages:
        result = client.list_workflow_runs(repository, page=page, per_page=per_page)
        if not result.ok:
            listing.complete = False
            listing.errors.append(
                f"failed to list workflow runs (page {page}, status {result.status}): {result.error}"
            )
            return listing

        payload = result.data if isinstance(result.data, dict) else {}
        raw_runs = payload.get("workflow_runs")
        if raw_runs is None:
            listing.complete = False
            listing.errors.append(f"workflow runs page {page} has no `workflow_runs` field")
            return listing
        if not isinstance(raw_runs, list):
            listing.complete = False
            listing.errors.append(f"workflow runs page {page} is not a list")
            return listing

        listing.pages_read = page
        total = payload.get("total_count")
        if isinstance(total, int) and not isinstance(total, bool):
            listing.reported_total = total

        for raw in raw_runs:
            run = WorkflowRun.from_api(raw)
            if run.id is None or not run.status:
                # Record it so it is visible and excluded, never silently dropped.
                listing.complete = False
                listing.errors.append(f"unparseable workflow run on page {page}: {raw!r}"[:500])
            listing.runs.append(run)

        if len(raw_runs) < per_page:
            break
        page += 1
    else:
        listing.complete = False
        listing.errors.append(
            f"stopped after {max_pages} pages while runs were still being returned; "
            "refusing to plan deletions from a truncated listing"
        )
        return listing

    if listing.reported_total is not None and len(listing.runs) < listing.reported_total:
        listing.complete = False
        listing.errors.append(
            f"collected {len(listing.runs)} runs but the API reported total_count="
            f"{listing.reported_total}; listing is incomplete"
        )
    return listing


@dataclass
class RetentionOutcome:
    deleted: list[int] = field(default_factory=list)
    planned: list[int] = field(default_factory=list)
    failed: list[tuple[int, str]] = field(default_factory=list)
    aborted_reason: Optional[str] = None
    stop_reason: Optional[str] = None

    @property
    def errors(self) -> list[str]:
        return [message for _, message in self.failed]


def apply_retention(
    client: Any,
    repository: str,
    plan: RetentionPlan,
    *,
    dry_run: bool,
    log: Optional[Callable[[str], None]] = None,
) -> RetentionOutcome:
    """Execute (or simulate) the deletions of a plan.

    On a permission failure the remaining deletions for this repository are
    abandoned rather than retried, and the failure is reported verbatim.
    """
    emit = log or (lambda msg: print(msg))
    outcome = RetentionOutcome(planned=[run.id for run in plan.delete if run.id is not None])

    if plan.aborted:
        outcome.aborted_reason = plan.abort_reason
        emit(f"::warning::{repository}: retention aborted — {plan.abort_reason}")
        return outcome

    for run in plan.delete:
        if run.id is None:  # pragma: no cover - excluded by plan_retention
            continue
        if dry_run:
            emit(f"[DRY RUN] would delete run {run.id} | {run.created_at} | {run.name}")
            continue
        result = client.delete_workflow_run(repository, run.id)
        if result.ok:
            outcome.deleted.append(run.id)
            emit(f"Deleted run {run.id} | {run.created_at} | {run.name}")
            continue
        outcome.failed.append((run.id, f"delete failed (status {result.status}): {result.error}"))
        if result.forbidden:
            outcome.stop_reason = (
                "GitHub refused the deletion (403). The cleanup token needs `Actions: Read and write` "
                f"on {repository}; no further runs were deleted in this repository."
            )
            emit(f"::error::{repository}: {outcome.stop_reason}")
            break
        emit(f"::warning::{repository}: failed to delete run {run.id}: {result.error}")

    return outcome


def summarise(plan: RetentionPlan, outcome: RetentionOutcome) -> dict:
    """Counters used by the report layer."""
    return {
        "runs_examined": plan.examined,
        "runs_retained": len(plan.keep),
        "runs_deleted": len(outcome.deleted),
        "runs_planned_deletions": len(outcome.planned),
        "runs_excluded": len(plan.excluded),
        "runs_failed": len(outcome.failed),
        "keep_latest": plan.keep_latest,
        "listing_complete": plan.complete,
        "aborted_reason": outcome.aborted_reason or plan.abort_reason,
        "stop_reason": outcome.stop_reason,
    }


def paginate_expected_pages(total: int, per_page: int = PER_PAGE) -> int:
    """Utility used by tests and diagnostics to reason about pagination."""
    if total <= 0:
        return 0
    return int(math.ceil(total / per_page))

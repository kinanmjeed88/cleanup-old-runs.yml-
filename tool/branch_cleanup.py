#!/usr/bin/env python3
"""
Branch cleanup and workflow runs cleanup with safety guarantees.

Implements the policy described in docs/ENGINEERING_REPORT_2026-10-16.md:
- Discovers default branch via GitHub API (never hardcoded)
- Lists all remote branches via API pagination + git ls-remote fallback
- Respects protected branches, open PRs, unmerged branches
- Proves merge via ancestor check, PR merged status, and empty diff
- Re-verifies immediately before delete
- Cleans up old completed workflow runs with pagination and rate-limit handling

Usage:
  python tool/branch_cleanup.py --repo kinanmjeed88/Scan-id --delete
  DRY_RUN=true python tool/branch_cleanup.py   # default: dry run, no deletion

This script is intended to be invoked from GitHub Actions with GITHUB_TOKEN.

Central multi-repository usage: tool/central_cleanup.py runs this script as a
subprocess with the working directory set to an isolated clone of the target
repository and GH_TOKEN set to the dedicated cleanup token. The branch decision
algorithm below is shared verbatim between both entry points; the central
orchestrator only reads the additive `--json-report` output.

For unit tests, core decision functions are pure and not requiring network.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import quote

# Allow both `python3 tool/branch_cleanup.py` and `python3 -m tool.branch_cleanup`.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tool.github_client import redact

DEFAULT_COMMAND_TIMEOUT = 60
GIT_LIST_TIMEOUT = 120
GIT_FETCH_TIMEOUT = 900
MAX_BRANCH_PAGES = 50
_HTTP_STATUS_RE = re.compile(r"HTTP\s+(\d{3})", re.IGNORECASE)
_RETRY_AFTER_RE = re.compile(r"retry-after[:\s]+(\d+)", re.IGNORECASE)
_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")


def _secret_token() -> str:
    return os.environ.get("CLEANUP_TOKEN") or os.environ.get("GH_TOKEN") or ""


def _redact(text: str) -> str:
    return redact(text or "", _secret_token())


def _http_status(stderr: str) -> Optional[int]:
    match = _HTTP_STATUS_RE.search(stderr or "")
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    return None


# Pure logic — no I/O — testable


@dataclass
class BranchInfo:
    name: str
    sha: Optional[str] = None
    protected: bool = False


@dataclass
class PRInfo:
    number: int
    head_ref: str
    state: str  # open, closed
    merged: bool
    merged_at: Optional[str] = None
    merge_commit_sha: Optional[str] = None
    base_ref: Optional[str] = None


@dataclass
class EvalResult:
    branch: str
    action: str  # 'keep' or 'delete'
    reason: str
    details: dict = field(default_factory=dict)


@dataclass
class BranchListing:
    """Structured result of branch discovery.

    ``complete`` is only true when a source was read successfully to its end.
    An empty list is not proof of an empty repository by itself; ``empty_verified``
    records that a successful API listing and a successful ``git ls-remote`` both
    agreed that no heads exist.
    """

    branches: list[BranchInfo] = field(default_factory=list)
    complete: bool = False
    source: str = "none"
    empty_verified: bool = False
    pages_read: int = 0
    http_status: Optional[int] = None
    returncode: Optional[int] = None
    stderr: str = ""
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "branch_count": len(self.branches),
            "complete": self.complete,
            "source": self.source,
            "empty_verified": self.empty_verified,
            "pages_read": self.pages_read,
            "http_status": self.http_status,
            "returncode": self.returncode,
            "stderr": _redact(self.stderr)[:2000],
            "error": _redact(self.error),
            "warnings": [_redact(w) for w in self.warnings],
            "attempts": list(self.attempts),
        }


def evaluate_branch(
    branch: BranchInfo,
    *,
    default_branch: str,
    open_pr_branches: set[str],
    protected_branches: set[str],
    # Merge evidence
    is_ancestor: Optional[bool],  # git merge-base --is-ancestor result, None if unknown
    has_merged_pr: bool,  # whether a closed merged PR exists for this branch
    merge_commit_is_ancestor: Optional[bool],
    diff_empty: Optional[bool],  # git diff default...branch empty
    fetch_error: Optional[str] = None,
    api_error: Optional[str] = None,
) -> EvalResult:
    """
    Decide whether a branch is eligible for deletion.

    Safety order (first match wins for 'keep'):
    1. fetch/api error -> keep
    2. default branch -> keep
    3. protected -> keep
    4. open PR -> keep
    5. not ancestor and no merged PR -> keep (unmerged)
    6. ancestor true -> delete (if not blocked above)
    7. has_merged_pr + merge_commit ancestor true -> delete (squash/rebase)
    8. diff_empty true + has_merged_pr true -> delete (squash fallback)
    9. otherwise keep

    This implements "proof of merge" not just mergeability.
    """
    if fetch_error or api_error:
        return EvalResult(
            branch.name,
            "keep",
            f"cannot verify safely: {fetch_error or api_error}",
            {"fetch_error": fetch_error, "api_error": api_error},
        )

    if branch.name == default_branch:
        return EvalResult(branch.name, "keep", "default branch", {"default": True})

    if branch.protected or branch.name in protected_branches:
        return EvalResult(branch.name, "keep", "protected branch", {"protected": True})

    if branch.name in open_pr_branches:
        return EvalResult(branch.name, "keep", "open pull request", {"open_pr": True})

    # Any branch with unique diff has unmerged changes — keep regardless of other evidence
    # This protects squash-merged branches that later received new commits
    if diff_empty is False:
        return EvalResult(
            branch.name,
            "keep",
            "unmerged: branch has unique changes not in default",
            {"diff_empty": False},
        )

    # If we could not determine ancestor status at all, keep
    if is_ancestor is None and not has_merged_pr:
        return EvalResult(
            branch.name,
            "keep",
            "cannot prove merge: ancestor check unavailable and no merged PR",
            {"is_ancestor": is_ancestor, "has_merged_pr": has_merged_pr},
        )

    # Direct ancestor => all commits are in default
    if is_ancestor is True:
        return EvalResult(
            branch.name,
            "delete",
            "merged: branch HEAD is ancestor of default branch",
            {"is_ancestor": True},
        )

    # Squash/Rebase case: PR was merged and merge commit is ancestor
    if has_merged_pr and merge_commit_is_ancestor is True:
        return EvalResult(
            branch.name,
            "delete",
            "merged: PR merged and merge commit is ancestor of default (squash/rebase)",
            {"has_merged_pr": True, "merge_commit_is_ancestor": True},
        )

    # Additional fallback: diff empty + merged PR suggests squash with no unique changes
    if diff_empty is True and has_merged_pr:
        return EvalResult(
            branch.name,
            "delete",
            "merged: no unique diff vs default and PR was merged (squash fallback)",
            {"diff_empty": True, "has_merged_pr": True},
        )

    # If diff_empty is False, there are still unique changes -> keep
    if diff_empty is False:
        return EvalResult(
            branch.name,
            "keep",
            "unmerged: branch has unique changes not in default",
            {"diff_empty": False},
        )

    # Default keep
    return EvalResult(
        branch.name,
        "keep",
        "cannot prove complete merge: not ancestor, no merge commit ancestor, diff unknown",
        {
            "is_ancestor": is_ancestor,
            "has_merged_pr": has_merged_pr,
            "merge_commit_is_ancestor": merge_commit_is_ancestor,
            "diff_empty": diff_empty,
        },
    )


def evaluate_all_branches(
    branches: list[BranchInfo],
    *,
    default_branch: str,
    open_pr_branches: set[str],
    protected_branches: set[str],
    merge_evidence: dict[str, dict],
) -> list[EvalResult]:
    """Batch evaluation for test harness."""
    results = []
    for b in branches:
        ev = merge_evidence.get(b.name, {})
        results.append(
            evaluate_branch(
                b,
                default_branch=default_branch,
                open_pr_branches=open_pr_branches,
                protected_branches=protected_branches,
                is_ancestor=ev.get("is_ancestor"),
                has_merged_pr=ev.get("has_merged_pr", False),
                merge_commit_is_ancestor=ev.get("merge_commit_is_ancestor"),
                diff_empty=ev.get("diff_empty"),
                fetch_error=ev.get("fetch_error"),
                api_error=ev.get("api_error"),
            )
        )
    return results


# I/O helpers (used in GitHub Actions, not in unit tests)


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a child process with a bounded timeout and diagnostic-safe output.

    A timeout or missing executable is returned as a non-zero CompletedProcess
    so callers can record the failure instead of hanging or crashing.
    """
    timeout = kwargs.pop("timeout", DEFAULT_COMMAND_TIMEOUT)
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"command timed out after {timeout}s")
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


def gh_api(endpoint: str, method: str = "GET", **kwargs) -> Optional[dict]:
    """Call gh api with rate-limit aware retry."""
    max_retries = 3
    for attempt in range(max_retries):
        proc = run(["gh", "api", endpoint, "--method", method] + kwargs.get("extra_args", []))
        if proc.returncode == 0:
            try:
                return json.loads(proc.stdout) if proc.stdout.strip() else {}
            except json.JSONDecodeError:
                # For non-JSON responses (e.g., DELETE)
                return {}
        stderr = proc.stderr.lower()
        # Retry rate limits and transient server errors, never ordinary permission
        # denials: a plain 403 is not made safer by repeating it.
        if any(code in stderr for code in ["429", "rate limit", "secondary rate"]) or (
            "403" in stderr and "rate" in stderr
        ):
            retry_after = _RETRY_AFTER_RE.search(proc.stderr or "")
            default_wait = int(retry_after.group(1)) if retry_after else 2 ** attempt
            wait = int(kwargs.get("retry_after", default_wait))
            print(f"::warning::GitHub API rate limited on {endpoint}, retry {attempt+1}/{max_retries} after {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue
        if "404" in stderr or "not found" in stderr:
            return None
        if attempt < max_retries - 1 and any(x in stderr for x in ["500", "502", "503", "504", "connection", "timed out"]):
            time.sleep(2 ** attempt)
            continue
        print(f"::warning::gh api {endpoint} failed: {_redact(proc.stderr.strip())}", file=sys.stderr)
        return None
    return None


def fetch_default_branch(repo: str) -> Optional[str]:
    proc = run(["gh", "api", f"repos/{repo}", "--jq", ".default_branch"])
    if proc.returncode == 0:
        branch = proc.stdout.strip()
        if branch:
            print(f"Default branch from API: {branch}")
            return branch
    # Fallback: git remote
    proc = run(["git", "remote", "show", "origin"])
    if proc.returncode == 0:
        for line in proc.stdout.splitlines():
            if "HEAD branch:" in line:
                return line.split(":")[-1].strip()
    # Fallback: origin/HEAD symbolic ref
    proc = run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"])
    if proc.returncode == 0:
        ref = proc.stdout.strip()
        return ref.split("/")[-1]
    return None


def _list_remote_branches_api_detailed(repo: str) -> BranchListing:
    """Paginate the branches API and keep completeness/diagnostic evidence."""
    result = BranchListing(source="api")
    seen: set[str] = set()
    page = 1
    while True:
        proc = run(["gh", "api", f"repos/{repo}/branches?per_page=100&page={page}", "--jq", ".[] | {name: .name, protected: .protected, sha: .commit.sha}"])
        result.returncode = proc.returncode
        result.stderr = (proc.stderr or "").strip()
        result.pages_read = page
        if proc.returncode != 0:
            result.http_status = _http_status(result.stderr)
            result.error = f"API branch listing failed on page {page} (exit {proc.returncode})"
            print(f"::warning::{_redact(result.error)}: {_redact(result.stderr)[:500]}", file=sys.stderr)
            return result
        result.http_status = 200
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        if not lines:
            result.complete = True
            return result
        for line in lines:
            try:
                obj = json.loads(line)
                name = obj["name"]
                if not isinstance(name, str) or not name:
                    raise ValueError("branch name missing")
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                result.error = f"invalid branch record on page {page}: {exc}"
                print(f"::warning::{_redact(result.error)}", file=sys.stderr)
                return result
            if name in seen:
                result.error = f"duplicate branch name in API pagination: {name}"
                print(f"::warning::{_redact(result.error)}", file=sys.stderr)
                return result
            seen.add(name)
            result.branches.append(BranchInfo(name=name, sha=obj.get("sha"), protected=bool(obj.get("protected", False))))
        if len(lines) < 100:
            result.complete = True
            return result
        page += 1
        if page > MAX_BRANCH_PAGES:
            result.error = f"branch pagination exceeded safety cap of {MAX_BRANCH_PAGES} pages"
            print(f"::warning::{result.error}", file=sys.stderr)
            return result


def _list_remote_branches_git_detailed() -> BranchListing:
    """Fallback/corroboration via ``git ls-remote --heads origin``.

    The child exit code is authoritative: a zero exit with empty stdout proves
    there are no heads; a non-zero exit (auth, network, missing remote, timeout)
    never proves emptiness.
    """
    result = BranchListing(source="git")
    proc = run(["git", "ls-remote", "--heads", "origin"], timeout=GIT_LIST_TIMEOUT)
    result.returncode = proc.returncode
    result.stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        result.error = f"git ls-remote failed (exit {proc.returncode})"
        print(f"::warning::{_redact(result.error)}: {_redact(result.stderr)[:500]}", file=sys.stderr)
        return result
    seen: set[str] = set()
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2 or not parts[1].startswith("refs/heads/"):
            result.error = "unexpected git ls-remote output format"
            return result
        sha, ref = parts
        name = ref.removeprefix("refs/heads/")
        if not _SHA_RE.fullmatch(sha) or not name or name in seen:
            result.error = f"invalid or duplicate git head record: {ref}"
            return result
        seen.add(name)
        result.branches.append(BranchInfo(name=name, sha=sha))
    result.complete = True
    result.empty_verified = not result.branches
    return result


def discover_branches(repo: str) -> BranchListing:
    """Resolve branch discovery with explicit empty-vs-failure semantics."""
    api = _list_remote_branches_api_detailed(repo)
    if api.complete and api.branches:
        api.attempts = [api.as_dict()]
        return api

    git = _list_remote_branches_git_detailed()
    api_attempt = api.as_dict()
    git_attempt = git.as_dict()
    if api.complete and not api.branches:
        if git.complete and not git.branches:
            api.source = "api+git"
            api.empty_verified = True
            api.warnings.append("empty branch list verified by API and git ls-remote")
            api.attempts = [api_attempt, git_attempt]
            return api
        if git.complete and git.branches:
            git.warnings.append("API listed zero branches but git ls-remote returned heads; using git")
            git.attempts = [api_attempt, git_attempt]
            return git
        api.complete = False
        api.error = "API returned an empty branch list but git ls-remote could not verify emptiness"
        api.warnings.append(git.error or "git verification unavailable")
        api.attempts = [api_attempt, git_attempt]
        return api

    if git.complete:
        git.warnings.append(api.error or "API branch listing incomplete; using git ls-remote")
        git.attempts = [api_attempt, git_attempt]
        return git
    api.warnings.append(git.error or "git ls-remote incomplete")
    api.error = api.error or "branch listing incomplete"
    api.attempts = [api_attempt, git_attempt]
    return api


def list_remote_branches_api(repo: str) -> tuple[list[BranchInfo], bool]:
    """Backward-compatible tuple wrapper around the detailed API listing."""
    listing = _list_remote_branches_api_detailed(repo)
    return listing.branches, listing.complete


def list_remote_branches_git_fallback() -> list[BranchInfo]:
    """Backward-compatible wrapper around the detailed git listing."""
    return _list_remote_branches_git_detailed().branches


def list_open_pr_branches(repo: str) -> tuple[set[str], bool]:
    """Return (open-pr refs, complete).  Missing an open PR could delete work."""
    refs: set[str] = set()
    page = 1
    while True:
        proc = run(["gh", "api", f"repos/{repo}/pulls?state=open&per_page=100&page={page}", "--jq", ".[].head.ref"])
        if proc.returncode != 0:
            return refs, False
        lines = [l.strip() for l in proc.stdout.splitlines() if l.strip()]
        if not lines:
            return refs, True
        refs.update(lines)
        if len(lines) < 100:
            return refs, True
        page += 1
        if page > 50:
            return refs, False


def list_closed_merged_prs(repo: str) -> dict[str, PRInfo]:
    """Map head_ref -> PRInfo for closed merged PRs (recent 500)."""
    prs: dict[str, PRInfo] = {}
    page = 1
    while True:
        proc = run([
            "gh", "api",
            f"repos/{repo}/pulls?state=closed&per_page=100&page={page}&sort=updated&direction=desc",
            "--jq", '.[] | select(.merged_at != null) | {number: .number, head_ref: .head.ref, state: .state, merged: true, merged_at: .merged_at, merge_commit_sha: .merge_commit_sha, base_ref: .base.ref}'
        ])
        if proc.returncode != 0:
            break
        lines = [l for l in proc.stdout.splitlines() if l.strip()]
        if not lines:
            break
        for line in lines:
            try:
                obj = json.loads(line)
                ref = obj.get("head_ref")
                if not ref:
                    continue
                # Keep latest merged PR per branch
                if ref not in prs:
                    prs[ref] = PRInfo(
                        number=obj["number"],
                        head_ref=ref,
                        state=obj["state"],
                        merged=True,
                        merged_at=obj.get("merged_at"),
                        merge_commit_sha=obj.get("merge_commit_sha"),
                        base_ref=obj.get("base_ref"),
                    )
            except Exception:
                continue
        if len(lines) < 100 or page >= 5:  # limit to 500 recent merged PRs
            break
        page += 1
    return prs


def check_protected_branches(branches: list[BranchInfo]) -> set[str]:
    return {b.name for b in branches if b.protected}


def ensure_full_history():
    # Ensure we have full history for ancestor checks
    run(["git", "fetch", "origin", "--prune", "--tags", "--prune-tags"], check=False, timeout=GIT_FETCH_TIMEOUT)
    # Fetch all heads to remotes/origin/*
    run(["git", "fetch", "origin", "+refs/heads/*:refs/remotes/origin/*"], check=False, timeout=GIT_FETCH_TIMEOUT)


def is_ancestor(branch: str, default_branch: str) -> Optional[bool]:
    # Check if origin/branch is ancestor of origin/default_branch
    proc = run(["git", "merge-base", "--is-ancestor", f"origin/{branch}", f"origin/{default_branch}"])
    if proc.returncode == 0:
        return True
    elif proc.returncode == 1:
        return False
    else:
        # Error (branch not fetched etc.)
        return None


def diff_empty(branch: str, default_branch: str) -> Optional[bool]:
    proc = run(["git", "diff", "--quiet", f"origin/{default_branch}...origin/{branch}"])
    if proc.returncode == 0:
        return True  # no diff
    elif proc.returncode == 1:
        return False  # has diff
    else:
        return None


def merge_commit_is_ancestor(merge_sha: str, default_branch: str) -> Optional[bool]:
    if not merge_sha:
        return None
    proc = run(["git", "merge-base", "--is-ancestor", merge_sha, f"origin/{default_branch}"])
    if proc.returncode == 0:
        return True
    elif proc.returncode == 1:
        return False
    else:
        # Try via API compare as fallback
        return None


def protection_status(repo: str, branch: str) -> str:
    """Return ``protected``, ``unprotected`` or ``unknown``.

    Only a conclusive HTTP 404 means unprotected.  403, 5xx, timeouts and any
    other error are ``unknown`` and must keep the branch.
    """
    encoded = quote(branch, safe="/")
    proc = run(["gh", "api", f"repos/{repo}/branches/{encoded}/protection"])
    if proc.returncode == 0:
        return "protected"
    stderr = (proc.stderr or "").lower()
    if "404" in stderr or "not found" in stderr:
        return "unprotected"
    return "unknown"


def refresh_remote_refs(branch: str, default_branch: str) -> bool:
    """Refresh the exact branch/default refs immediately before re-verification.

    Git refspecs take raw ref names; percent-encoding is only for API URL paths.
    Encoding here would corrupt valid names containing ``%``, ``#`` or spaces.
    """
    proc = run([
        "git", "fetch", "origin",
        f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
        f"+refs/heads/{default_branch}:refs/remotes/origin/{default_branch}",
    ], timeout=GIT_FETCH_TIMEOUT)
    return proc.returncode == 0


def delete_branch_remote(repo: str, branch: str, dry_run: bool = True) -> bool:
    if dry_run:
        print(f"[DRY RUN] Would delete branch: {branch}")
        return True
    print(f"Deleting branch {branch} through the scoped API...")
    encoded = quote(branch, safe="/")
    proc = run(["gh", "api", "--method", "DELETE", f"repos/{repo}/git/refs/heads/{encoded}"])
    if proc.returncode == 0:
        print(f"Deleted branch {branch}")
        return True
    stderr = (proc.stderr or "").lower()
    if "404" in stderr or "not found" in stderr:
        print(f"Branch {branch} was already absent (HTTP 404); desired end state reached")
        return True
    # Never fall back to a Git push for deletion. A failed API request is
    # inconclusive; keep the branch and require a fresh, explicit review.
    print(f"::warning::Failed to delete {branch} through the scoped API: {_redact(proc.stderr)}", file=sys.stderr)
    return False


def cleanup_runs(repo: str, cutoff_hours: int = 24, dry_run: bool = True) -> tuple[int, int, int]:
    """Legacy time-based run cleanup (kept for CLI compatibility).

    Deprecated: the central system keeps the newest N completed runs per
    repository via tool/run_retention.py instead of using a time cutoff.
    """
    print(
        "::warning::legacy time-based run cleanup is deprecated; "
        "use tool/central_cleanup.py (newest-N retention) instead",
        file=sys.stderr,
    )
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cutoff_hours)
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    current_run_id = os.environ.get("GITHUB_RUN_ID", "")
    print(f"Deleting workflow runs created before: {cutoff_str} (cutoff {cutoff_hours}h)")
    page = 1
    deleted = 0
    skipped = 0
    failed = 0
    while True:
        proc = run(["gh", "api", f"repos/{repo}/actions/runs?per_page=100&page={page}", "--jq", "[.workflow_runs[] | {id: .id, created_at: .created_at, status: .status, name: .name}]"])
        if proc.returncode != 0:
            print(f"::warning::Failed to list runs page {page}: {_redact(proc.stderr)}", file=sys.stderr)
            break
        try:
            runs = json.loads(proc.stdout) if proc.stdout.strip() else []
        except Exception:
            runs = []
        if not runs:
            break
        for r in runs:
            run_id = str(r.get("id"))
            created = r.get("created_at", "")
            status = r.get("status", "")
            name = r.get("name", "")
            if run_id == current_run_id:
                skipped += 1
                continue
            if status != "completed":
                skipped += 1
                continue
            if created >= cutoff_str:
                skipped += 1
                continue
            if dry_run:
                print(f"[DRY RUN] would delete run {run_id} | {created} | {name}")
                continue
            print(f"Deleting run {run_id} | {created} | {name}")
            del_proc = run(["gh", "api", "--method", "DELETE", f"repos/{repo}/actions/runs/{run_id}"])
            if del_proc.returncode == 0:
                deleted += 1
            else:
                print(f"::warning::Failed to delete run {run_id}: {_redact(del_proc.stderr)}", file=sys.stderr)
                failed += 1
        if len(runs) < 100:
            break
        page += 1
        if page > 100:
            print("::warning::Too many pages for runs, stopping", file=sys.stderr)
            break
    print(f"Runs cleanup: deleted={deleted} skipped={skipped} failed={failed} dry_run={dry_run}")
    return deleted, skipped, failed


def _write_json_report(path: str, payload: dict) -> None:
    """Write the machine-readable branch report used by the central orchestrator.

    The payload contains no credentials: it is built from branch names, actions
    and reasons only.
    """
    if not path:
        return
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Wrote JSON report: {target}")
    except OSError as exc:
        print(f"::warning::Could not write JSON report {path}: {exc}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Branch and runs cleanup with safety")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY") or os.environ.get("REPOSITORY") or "")
    parser.add_argument("--delete", action="store_true", help="Actually delete (default dry-run)")
    parser.add_argument("--cutoff-hours", type=int, default=24, help="Hours for run cleanup cutoff")
    parser.add_argument("--skip-runs", action="store_true", help="Skip runs cleanup")
    parser.add_argument("--skip-branches", action="store_true", help="Skip branch cleanup")
    parser.add_argument("--dry-run", action="store_true", default=None, help="Force dry-run even if --delete")
    parser.add_argument(
        "--json-report",
        default="",
        help="Optional path for a machine-readable branch decision report (contains no secrets)",
    )
    args = parser.parse_args()

    repo = args.repo or os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        print("::error::Repository not specified (set GITHUB_REPOSITORY or --repo)", file=sys.stderr)
        sys.exit(1)

    report: dict = {
        "repo": repo,
        "dry_run": None,
        "mode": "",
        "default_branch": None,
        "branches_examined": 0,
        "kept": 0,
        "deleted": 0,
        "planned_deletions": 0,
        "failed": 0,
        "results": [],
        "errors": [],
        "branches_complete": False,
        "open_pr_complete": False,
        "empty_verified": False,
        "branch_listing": {},
    }

    # DRY_RUN env overrides: if DELETE_BRANCHES != true, stay dry
    env_delete = os.environ.get("DELETE_BRANCHES", "").lower() in ("1", "true", "yes")
    dry_run = not (args.delete or env_delete)
    if args.dry_run:
        dry_run = True
    report["dry_run"] = bool(dry_run)
    report["mode"] = "DRY RUN" if dry_run else "LIVE"
    if dry_run:
        print("Running in DRY RUN mode — no branches or runs will be deleted. Set --delete or DELETE_BRANCHES=true to enable.")

    # Ensure full git history for ancestor checks
    if not args.skip_branches:
        ensure_full_history()

    default_branch = fetch_default_branch(repo)
    if not default_branch:
        report["errors"].append("could not determine the default branch")
        _write_json_report(args.json_report, report)
        print("::error::Could not determine default branch", file=sys.stderr)
        sys.exit(1)
    report["default_branch"] = default_branch
    print(f"Repository: {repo}")
    print(f"Default branch: {default_branch}")

    # Branch cleanup
    if not args.skip_branches:
        listing = discover_branches(repo)
        branches_api = listing.branches
        branches_complete = listing.complete
        report["branch_listing"] = listing.as_dict()
        report["branches_complete"] = branches_complete
        report["empty_verified"] = listing.empty_verified

        protected_set = check_protected_branches(branches_api)
        open_pr_set, open_pr_complete = list_open_pr_branches(repo)
        report["open_pr_complete"] = open_pr_complete
        merged_prs = list_closed_merged_prs(repo) if branches_api else {}

        if not branches_complete or (branches_api and not open_pr_complete):
            message = (
                "branch safety listing is incomplete "
                f"(branches_complete={branches_complete}, open_pr_complete={open_pr_complete}); "
                "refusing branch deletions"
            )
            if listing.error:
                message += f": {_redact(listing.error)}"
            report["errors"].append(message)
            print(f"::error::{message}", file=sys.stderr)
            _write_json_report(args.json_report, report)
            sys.exit(1)

        if listing.empty_verified:
            print("Verified empty repository: API and git ls-remote agree there are no branch heads")
        if not branches_api and not open_pr_complete:
            print("::warning::Open PR listing incomplete, but no branch heads exist to delete", file=sys.stderr)
        print(f"Found {len(branches_api)} remote branches")
        print(f"Protected: {protected_set}")
        print(f"Open PR branches: {open_pr_set}")
        print(f"Merged PRs mapped: {list(merged_prs.keys())[:10]}")

        results: list[EvalResult] = []
        deleted = 0
        kept = 0
        failed = 0

        for branch in branches_api:
            # Skip default immediately
            if branch.name == default_branch:
                results.append(EvalResult(branch.name, "keep", "default branch"))
                kept += 1
                continue

            # Gather evidence
            anc = is_ancestor(branch.name, default_branch)
            diff = diff_empty(branch.name, default_branch)
            pr_info = merged_prs.get(branch.name)
            has_merged = pr_info is not None
            merge_anc = None
            if pr_info and pr_info.merge_commit_sha:
                merge_anc = merge_commit_is_ancestor(pr_info.merge_commit_sha, default_branch)

            # Determine protected via API flag already in branch.protected
            protected_flag = branch.protected

            res = evaluate_branch(
                branch,
                default_branch=default_branch,
                open_pr_branches=open_pr_set,
                protected_branches=protected_set,
                is_ancestor=anc,
                has_merged_pr=has_merged,
                merge_commit_is_ancestor=merge_anc,
                diff_empty=diff,
            )

            # Re-verify protected before delete (defense in depth)
            if res.action == "delete":
                # Only a conclusive 404 from the protection endpoint allows a
                # deletion.  403, 5xx, timeouts and unknown errors keep safely.
                prot = protection_status(repo, branch.name)
                if prot == "protected":
                    res = EvalResult(branch.name, "keep", "protected (re-verified via API)", {"protected": True})
                    kept += 1
                    print(f"KEEP {branch.name}: {res.reason}")
                    results.append(res)
                    continue
                if prot != "unprotected":
                    res = EvalResult(branch.name, "keep", f"cannot verify protection ({prot}) — keep safely", {"protection_status": prot})
                    kept += 1
                    print(f"KEEP {branch.name}: {res.reason}")
                    results.append(res)
                    continue

                # Refresh the exact remote refs so the re-verification cannot
                # act on a stale clone if the branch/default moved during scan.
                if not refresh_remote_refs(branch.name, default_branch):
                    res = EvalResult(branch.name, "keep", "cannot refresh remote refs before delete — keep safely", {"fetch_error": True})
                    kept += 1
                    print(f"KEEP {branch.name}: {res.reason}")
                    results.append(res)
                    continue

                anc2 = is_ancestor(branch.name, default_branch)
                diff2 = diff_empty(branch.name, default_branch)
                merge_anc2 = None
                if pr_info and pr_info.merge_commit_sha:
                    merge_anc2 = merge_commit_is_ancestor(pr_info.merge_commit_sha, default_branch)
                still_proven = (
                    anc2 is True
                    or (has_merged and merge_anc2 is True)
                    or (has_merged and diff2 is True)
                )
                if diff2 is False or not still_proven:
                    res = EvalResult(branch.name, "keep", f"branch changed since evaluation (ancestor {anc} -> {anc2}, diff {diff} -> {diff2})", {"race": True})
                    kept += 1
                    print(f"KEEP {branch.name}: {res.reason}")
                    results.append(res)
                    continue

                success = delete_branch_remote(repo, branch.name, dry_run=dry_run)
                if success:
                    deleted += 1
                else:
                    failed += 1
                results.append(res)
                print(f"{res.action.upper()} {branch.name}: {res.reason} — {res.details}")
            else:
                kept += 1
                print(f"KEEP {branch.name}: {res.reason} — {res.details}")
                results.append(res)

        print("\n=== Branch cleanup summary ===")
        print(f"Total branches examined: {len(branches_api)}")
        print(f"Deleted: {deleted} (dry_run={dry_run})")
        print(f"Kept: {kept}")
        print(f"Failed deletions: {failed}")
        for r in results:
            print(f"{r.action} {r.branch}: {r.reason}")

        # Persist summary for Actions
        summary_path = Path(os.environ.get("GITHUB_STEP_SUMMARY", ""))
        if summary_path and summary_path.parent.exists():
            try:
                with open(summary_path, "a", encoding="utf-8") as f:
                    f.write(f"### Branch cleanup ({'DRY RUN' if dry_run else 'LIVE'})\n")
                    f.write(f"- Default branch: `{default_branch}`\n")
                    f.write(f"- Total: {len(branches_api)}, Deleted: {deleted}, Kept: {kept}, Failed: {failed}\n")
                    f.write("| Branch | Action | Reason |\n|---|---|---|\n")
                    for r in results:
                        f.write(f"| {r.branch} | {r.action} | {r.reason} |\n")
            except Exception:
                pass

        report["branches_examined"] = len(branches_api)
        report["kept"] = kept
        report["planned_deletions"] = sum(1 for r in results if r.action == "delete")
        report["deleted"] = 0 if dry_run else deleted
        report["failed"] = failed
        report["results"] = [
            {"branch": r.branch, "action": r.action, "reason": r.reason} for r in results
        ]
        if failed:
            report["errors"].append(f"{failed} branch deletion(s) failed")

    # Runs cleanup
    if not args.skip_runs:
        deleted, skipped, failed = cleanup_runs(repo, cutoff_hours=args.cutoff_hours, dry_run=dry_run)
        print(f"Runs summary: deleted={deleted} skipped={skipped} failed={failed}")

    _write_json_report(args.json_report, report)
    print("Cleanup finished.")
    if report.get("errors") or report.get("failed"):
        sys.exit(1)


if __name__ == "__main__":
    main()

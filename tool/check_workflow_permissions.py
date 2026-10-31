#!/usr/bin/env python3
"""Least-privilege gate for GitHub Actions workflows.

Rules
-----
R001  the file must be valid YAML and describe a workflow mapping
R002  a top level ``permissions:`` block must be declared (never the default)
R003  ``write-all`` is forbidden everywhere
R004  permission values must be one of ``read``, ``write``, ``none``
R005  every permission scope must be a real GitHub Actions scope
R006  a job holding a write scope must be guarded to the default branch
      (``if:`` expression mentioning ``default_branch``), so a pull-request or
      feature ref can never obtain write capabilities
R007  a workflow triggered by ``schedule`` must not request any write scope —
      scheduled runs are unattended and must stay read-only
R008  a workflow that references ``secrets.CLEANUP_TOKEN`` must not grant write
      scopes to the workflow token: deletion capability belongs to the
      dedicated cleanup token only

Usage::

    python3 tool/check_workflow_permissions.py [.github/workflows]

Exit code 0 when no violations are found, 1 otherwise.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

try:
    import yaml
except ImportError:  # pragma: no cover - dependency is declared in requirements-dev.txt
    print("::error::PyYAML is required: pip install -r requirements-dev.txt", file=sys.stderr)
    raise SystemExit(2)

DEFAULT_WORKFLOW_DIR = Path(".github/workflows")

KNOWN_SCOPES = frozenset(
    {
        "actions",
        "attestations",
        "checks",
        "contents",
        "deployments",
        "discussions",
        "id-token",
        "issues",
        "models",
        "packages",
        "pages",
        "pull-requests",
        "repository-projects",
        "security-events",
        "statuses",
    }
)

ALLOWED_VALUES = frozenset({"read", "write", "none"})

#: Scopes that grant mutation power over the repository.
DESTRUCTIVE_SCOPES = frozenset({"actions", "contents", "packages", "pages", "workflows"})


@dataclass(frozen=True)
class Violation:
    path: str
    rule: str
    message: str
    job: Optional[str] = None

    def __str__(self) -> str:
        location = f"{self.path}"
        if self.job:
            location += f" [{self.job}]"
        return f"{self.rule} {location}: {self.message}"


def _normalise_triggers(raw: Any) -> dict:
    """PyYAML parses the bare ``on:`` key as boolean True (YAML 1.1)."""
    if isinstance(raw, dict):
        return raw
    return {}


def _workflow_body(document: dict) -> tuple[dict, dict]:
    triggers = document.get("on")
    if triggers is None:
        triggers = document.get(True)  # noqa: FBT003 - YAML boolean key
    jobs = document.get("jobs")
    return _normalise_triggers(triggers), jobs if isinstance(jobs, dict) else {}


def _permission_map(raw: Any) -> dict[str, str]:
    if raw is None:
        return {}
    if isinstance(raw, str):
        return {"__global__": raw}
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    return {"__global__": str(raw)}


def _has_write(mapping: dict[str, str]) -> bool:
    return any(value == "write" for value in mapping.values())


def _job_guard(job: dict) -> str:
    value = job.get("if")
    return value if isinstance(value, str) else ""


def check_workflow_text(text: str, *, path: str) -> list[Violation]:
    violations: list[Violation] = []
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return [Violation(path, "R001", f"invalid YAML: {exc}")]

    if not isinstance(document, dict):
        return [Violation(path, "R001", "workflow file is not a YAML mapping")]

    triggers, jobs = _workflow_body(document)
    scheduled = "schedule" in triggers
    uses_cleanup_token = "secrets.CLEANUP_TOKEN" in text

    if "permissions" not in document:
        violations.append(
            Violation(path, "R002", "missing top-level `permissions:` block (defaults are broad)")
        )
        workflow_permissions: dict[str, str] = {}
    else:
        workflow_permissions = _permission_map(document.get("permissions"))
        if workflow_permissions.get("__global__") == "write-all":
            violations.append(Violation(path, "R003", "`write-all` is forbidden at workflow level"))
        for scope, value in workflow_permissions.items():
            if scope == "__global__":
                if value not in ("read-all", "none", "write-all"):
                    violations.append(
                        Violation(path, "R004", f"invalid global permission value {value!r}")
                    )
                continue
            if scope not in KNOWN_SCOPES:
                violations.append(Violation(path, "R005", f"unknown permission scope {scope!r}"))
            if value not in ALLOWED_VALUES:
                violations.append(
                    Violation(path, "R004", f"invalid value {value!r} for scope {scope!r}")
                )

    effective_workflow_write = bool(_has_write(workflow_permissions))
    if scheduled and effective_workflow_write:
        violations.append(
            Violation(
                path,
                "R007",
                "scheduled workflows must be read-only, but the workflow-level permissions "
                f"request write access: {sorted(workflow_permissions)}",
            )
        )
    if uses_cleanup_token and effective_workflow_write:
        violations.append(
            Violation(
                path,
                "R008",
                "this workflow uses secrets.CLEANUP_TOKEN, so the workflow token must stay read-only "
                f"(found write scopes: {sorted(k for k, v in workflow_permissions.items() if v == 'write')})",
            )
        )

    for name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        if "permissions" not in job:
            job_permissions = workflow_permissions
        else:
            job_permissions = _permission_map(job.get("permissions"))

        if job_permissions.get("__global__") == "write-all":
            violations.append(Violation(path, "R003", "`write-all` is forbidden at job level", str(name)))
        for scope, value in job_permissions.items():
            if scope == "__global__":
                continue
            if scope not in KNOWN_SCOPES:
                violations.append(
                    Violation(path, "R005", f"unknown permission scope {scope!r}", str(name))
                )
            if value not in ALLOWED_VALUES:
                violations.append(
                    Violation(path, "R004", f"invalid value {value!r} for scope {scope!r}", str(name))
                )

        write_scopes = sorted(
            scope for scope, value in job_permissions.items() if value == "write" and scope != "__global__"
        )
        if write_scopes:
            guard = _job_guard(job)
            if "default_branch" not in guard:
                violations.append(
                    Violation(
                        path,
                        "R006",
                        f"write scopes {write_scopes} require a job guard restricting execution to the "
                        "default branch (`if:` mentioning default_branch)",
                        str(name),
                    )
                )
            if scheduled:
                violations.append(
                    Violation(
                        path,
                        "R007",
                        f"scheduled workflow job requests write scopes {write_scopes}; "
                        "unattended runs must stay read-only",
                        str(name),
                    )
                )
            if uses_cleanup_token:
                violations.append(
                    Violation(
                        path,
                        "R008",
                        f"job requests workflow-token write scopes {write_scopes} while using "
                        "secrets.CLEANUP_TOKEN; deletion must rely on the dedicated token",
                        str(name),
                    )
                )
    return violations


def check_file(path: Path) -> list[Violation]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [Violation(str(path), "R001", f"cannot read file: {exc}")]
    return check_workflow_text(text, path=str(path))


def discover_workflows(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    if not target.exists():
        return []
    return sorted(
        file
        for file in target.iterdir()
        if file.is_file() and file.suffix in (".yml", ".yaml")
    )


def check_workflows(targets: Iterable[Path]) -> list[Violation]:
    violations: list[Violation] = []
    for target in targets:
        for path in discover_workflows(Path(target)):
            violations.extend(check_file(path))
    return violations


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Check GitHub Actions workflow permissions")
    parser.add_argument("paths", nargs="*", default=[str(DEFAULT_WORKFLOW_DIR)])
    args = parser.parse_args(argv)

    targets = [Path(p) for p in (args.paths or [str(DEFAULT_WORKFLOW_DIR)])]
    found = [path for target in targets for path in discover_workflows(target)]
    if not found:
        print(f"::error::no workflow files found under {', '.join(str(t) for t in targets)}", file=sys.stderr)
        return 1

    violations = check_workflows(targets)
    for path in found:
        print(f"checked {path}")
    if violations:
        for violation in violations:
            print(f"::error::{violation}")
        print(f"\n{len(violations)} workflow permission violation(s) found", file=sys.stderr)
        return 1

    print(f"\nAll {len(found)} workflow file(s) satisfy the least-privilege policy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

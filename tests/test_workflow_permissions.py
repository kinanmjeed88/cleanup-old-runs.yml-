"""Tests for the workflow least-privilege gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from tool.check_workflow_permissions import (
    check_file,
    check_workflow_text,
    check_workflows,
    main,
)

VALID = """
name: example
on:
  push:
permissions:
  contents: read
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""

WRITE_AND_GUARDED = """
name: example
on:
  workflow_dispatch:
permissions:
  contents: read
jobs:
  cleanup:
    if: ${{ github.ref == format('refs/heads/{0}', github.event.repository.default_branch) }}
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - run: echo hi
"""


def rules(text: str) -> set[str]:
    return {violation.rule for violation in check_workflow_text(text, path="test.yml")}


def test_valid_workflow_passes():
    assert check_workflow_text(VALID, path="test.yml") == []


def test_invalid_yaml_is_reported():
    assert "R001" in rules("name: [unclosed\n")


def test_missing_top_level_permissions():
    text = VALID.replace("permissions:\n  contents: read\n", "")
    assert "R002" in rules(text)


def test_write_all_is_forbidden():
    text = VALID.replace("permissions:\n  contents: read", "permissions: write-all")
    assert "R003" in rules(text)


def test_invalid_permission_value():
    text = VALID.replace("contents: read", "contents: readwrite")
    assert "R004" in rules(text)


def test_unknown_scope():
    text = VALID.replace("contents: read", "contents: read\n  teleport: read")
    assert "R005" in rules(text)


def test_write_scope_requires_default_branch_guard():
    text = """
name: example
on:
  push:
permissions:
  contents: read
jobs:
  cleanup:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - run: echo hi
"""
    assert "R006" in rules(text)


def test_guarded_write_scope_is_allowed():
    assert check_workflow_text(WRITE_AND_GUARDED, path="test.yml") == []


def test_scheduled_workflow_must_be_read_only():
    text = """
name: example
on:
  schedule:
    - cron: "0 2 * * *"
permissions:
  contents: write
jobs:
  cleanup:
    if: ${{ github.ref == format('refs/heads/{0}', github.event.repository.default_branch) }}
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""
    assert "R007" in rules(text)


def test_cleanup_token_requires_read_only_workflow_token():
    text = """
name: example
on:
  workflow_dispatch:
permissions:
  contents: write
jobs:
  cleanup:
    if: ${{ github.ref == format('refs/heads/{0}', github.event.repository.default_branch) }}
    runs-on: ubuntu-latest
    env:
      CLEANUP_TOKEN: ${{ secrets.CLEANUP_TOKEN }}
    steps:
      - run: echo hi
"""
    assert "R008" in rules(text)


def test_repository_workflows_pass_the_policy(repo_root: Path):
    violations = check_workflows([repo_root / ".github" / "workflows"])
    assert violations == [], "\n".join(str(v) for v in violations)


def test_check_file_reports_missing_file(tmp_path: Path):
    violations = check_file(tmp_path / "nope.yml")
    assert violations and violations[0].rule == "R001"


def test_main_succeeds_on_repository_workflows(repo_root: Path, capsys):
    assert main([str(repo_root / ".github" / "workflows")]) == 0
    assert "satisfy the least-privilege policy" in capsys.readouterr().out


def test_main_fails_on_violation(tmp_path: Path, capsys):
    (tmp_path / "bad.yml").write_text("on: push\njobs: {}\n", encoding="utf-8")
    assert main([str(tmp_path)]) == 1
    assert "violation" in capsys.readouterr().err


def test_main_errors_when_no_workflows(tmp_path: Path):
    assert main([str(tmp_path / "empty")]) == 1


@pytest.mark.parametrize("trigger", ["push:", "pull_request:"])
def test_pull_request_and_push_workflows_are_checked(repo_root: Path, trigger: str):
    text = (repo_root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert trigger in text
    assert check_workflow_text(text, path="ci.yml") == []

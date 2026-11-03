"""The 50-completed-runs-per-repository contract must not drift."""

from __future__ import annotations

from pathlib import Path

import yaml

from tool import central_cleanup
from tool.run_retention import DEFAULT_KEEP_LATEST

ROOT = Path(__file__).resolve().parents[1]


def test_python_defaults_are_fifty():
    assert DEFAULT_KEEP_LATEST == 50
    assert central_cleanup.DEFAULT_KEEP_LATEST == 50


def test_workflow_default_and_env_are_fifty():
    doc = yaml.safe_load((ROOT / ".github/workflows/cleanup-old-runs.yml").read_text(encoding="utf-8"))
    inputs = doc[True]["workflow_dispatch"]["inputs"]
    assert inputs["keep_latest"]["default"] == "50"
    job = doc["jobs"]["cleanup"]
    cleanup_steps = [s for s in job["steps"] if s.get("name") == "Run central cleanup"]
    assert cleanup_steps, "central cleanup step not found"
    step = cleanup_steps[0]
    assert step["env"]["KEEP_LATEST"] == "${{ inputs.keep_latest }}"
    assert '"--keep-latest" "${KEEP_LATEST:-50}"' in step["run"]


def test_active_code_and_docs_do_not_reintroduce_keep_twenty():
    targets = [
        ROOT / "tool",
        ROOT / ".github/workflows",
        ROOT / "README.md",
        ROOT / "docs/CENTRAL_CLEANUP.md",
        ROOT / "docs/ENGINEERING_REPORT.md",
    ]
    forbidden = ('keep-latest=20', 'keep-latest 20', 'KEEP_LATEST: "20"', "keep_latest || '20'", 'default: "20"')
    offenders = []
    for target in targets:
        files = target.rglob("*") if target.is_dir() else [target]
        for path in files:
            if not path.is_file() or path.suffix not in {".py", ".yml", ".yaml", ".md"}:
                continue
            text = path.read_text(encoding="utf-8")
            for marker in forbidden:
                if marker in text:
                    offenders.append(f"{path.relative_to(ROOT)}: {marker}")
    assert offenders == []


def test_readme_documents_fifty_completed_runs_per_repository():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "50 completed GitHub Actions runs per repository" in text

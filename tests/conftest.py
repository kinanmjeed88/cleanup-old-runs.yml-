"""Shared test fixtures.

The cleanup tests never touch the network and never delete anything: every test
either exercises pure logic, or drives the orchestrator with a fake API client
and a fake branch runner.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture()
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture()
def token() -> str:
    return "ghp_EXAMPLEtokenVALUEforTESTINGonly1234567890"

#!/usr/bin/env python3
"""Isolated git context per target repository.

``tool/branch_cleanup.py`` proves merges with local git commands against the
``origin`` remote (``git merge-base --is-ancestor origin/<branch>
origin/<default>`` and ``git diff origin/<default>...origin/<branch>``). When the
cleanup runs from a central repository, those commands must never see the
central repository's refs.

This module therefore hands every target repository its own throw-away full
clone, and the branch cleanup runs with that clone as its working directory.
Authentication uses a credential helper that reads ``$CLEANUP_TOKEN`` from the
environment, so the token never appears in a remote URL, on a command line, or
in a file.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional

from tool.github_client import build_child_env, redact
from tool.repositories import is_valid_repository

#: Credential helper executed by git. It reads the token from the environment,
#: so no secret is ever persisted or placed on a command line.
CREDENTIAL_HELPER = (
    '!f() { test "$1" = get || return 0; '
    'printf "username=x-access-token\\npassword=%s\\n" "$CLEANUP_TOKEN"; }; f'
)

DEFAULT_CLONE_TIMEOUT = 900


class WorkspaceError(RuntimeError):
    """Raised when an isolated git context cannot be created."""


@dataclass
class GitWorkspace:
    repository: str
    path: Path
    url: str
    warnings: list[str] = field(default_factory=list)


class GitWorkspaceFactory:
    """Creates and disposes an isolated full clone for one repository."""

    def __init__(
        self,
        *,
        token: str,
        base_dir: Optional[str | Path] = None,
        clone_timeout: int = DEFAULT_CLONE_TIMEOUT,
        runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._token = token
        self.base_dir = Path(base_dir) if base_dir else Path(tempfile.gettempdir())
        self.clone_timeout = clone_timeout
        self._runner = runner or subprocess.run
        self._log = log or (lambda msg: print(msg))

    @property
    def token(self) -> str:
        return self._token

    def _env(self) -> dict[str, str]:
        return build_child_env(
            self._token,
            extra={"GIT_LFS_SKIP_SMUDGE": "1"},
        )

    def _git(self, args: list[str], *, timeout: int) -> subprocess.CompletedProcess:
        try:
            return self._runner(
                ["git", *args],
                capture_output=True,
                text=True,
                env=self._env(),
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:  # pragma: no cover - environment specific
            raise WorkspaceError("the `git` executable is required but was not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise WorkspaceError(f"git command timed out after {timeout}s") from exc

    @contextmanager
    def create(self, repository: str) -> Iterator[GitWorkspace]:
        if not is_valid_repository(repository):
            raise WorkspaceError(f"refusing to create a workspace for invalid repository name {repository!r}")

        self.base_dir.mkdir(parents=True, exist_ok=True)
        safe_name = repository.replace("/", "__")
        path = Path(tempfile.mkdtemp(prefix=f"cleanup-{safe_name}-", dir=str(self.base_dir)))
        url = f"https://github.com/{repository}.git"
        workspace = GitWorkspace(repository=repository, path=path, url=url)

        try:
            self._log(f"Creating isolated clone for {repository} at {path}")
            clone = self._git(
                [
                    "-c", "credential.helper=",
                    "-c", f"credential.helper={CREDENTIAL_HELPER}",
                    "-c", "advice.detachedHead=false",
                    "-c", "core.pager=cat",
                    "clone", "--no-checkout", "--quiet", url, str(path),
                ],
                timeout=self.clone_timeout,
            )
            if clone.returncode != 0:
                raise WorkspaceError(
                    f"failed to clone {repository}: {redact(clone.stderr.strip(), self._token)}"
                )

            # Persist *only* the env-reading helper for the child process, and
            # reset any inherited helpers first so a global credential store can
            # never be used with a different identity.
            self._git(["-C", str(path), "config", "--local", "--add", "credential.helper", ""], timeout=60)
            self._git(
                ["-C", str(path), "config", "--local", "--add", "credential.helper", CREDENTIAL_HELPER],
                timeout=60,
            )
            self._git(["-C", str(path), "config", "--local", "gc.auto", "0"], timeout=60)

            # Full history and every branch, so ancestor/diff proofs are exact.
            fetch = self._git(
                [
                    "-C", str(path),
                    "-c", "credential.helper=",
                    "-c", f"credential.helper={CREDENTIAL_HELPER}",
                    "fetch", "--quiet", "--prune", "--no-tags", "origin",
                    "+refs/heads/*:refs/remotes/origin/*",
                ],
                timeout=self.clone_timeout,
            )
            if fetch.returncode != 0:
                workspace.warnings.append(
                    f"could not refresh remote branches: {redact(fetch.stderr.strip(), self._token)}"
                )

            yield workspace
        finally:
            shutil.rmtree(path, ignore_errors=True)


def credential_helper_uses_env_only(helper: str = CREDENTIAL_HELPER) -> bool:
    """True when the helper reads the token from the environment only.

    Used as an assertion in tests: the helper must never embed a literal token.
    """
    lowered = helper.lower()
    if "ghp_" in lowered or "github_pat_" in lowered:
        return False
    return "$cleanup_token" in lowered

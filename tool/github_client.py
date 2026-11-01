#!/usr/bin/env python3
"""Central GitHub API transport.

Design rules enforced here:

* The access token is read from the environment (``CLEANUP_TOKEN`` by default).
  It is never written to disk, never embedded in a URL or command-line argument
  and never printed.
* Child processes receive the token only through their environment. Any
  inherited ``GH_TOKEN`` / ``GITHUB_TOKEN`` (i.e. the central repository's own
  ``github.token``) is stripped, so the central token can never silently act as
  a substitute for the dedicated cleanup token.
* Every string that leaves this module (logs, exceptions, reports) is passed
  through :func:`redact`.

This module performs no policy decisions; it only moves bytes to and from the
GitHub API. All decisions live in ``tool/run_retention.py`` (runs) and
``tool/branch_cleanup.py`` (branches).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

TOKEN_ENV_VAR = "CLEANUP_TOKEN"

#: Variables that could make a child process talk to GitHub with a *different*
#: identity than the dedicated cleanup token.
FORBIDDEN_ENV_VARS = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
)

REDACTED = "***REDACTED***"

_TOKEN_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{16,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"),
)

_HTTP_STATUS_RE = re.compile(r"HTTP[ /](\d{3})", re.IGNORECASE)


class CleanupTokenError(RuntimeError):
    """Raised when the dedicated cleanup token is missing or unusable."""


class GhUnavailableError(RuntimeError):
    """Raised when the ``gh`` CLI cannot be executed at all."""


def redact(text: Optional[str], token: Optional[str] = None) -> str:
    """Return ``text`` with credentials removed.

    ``token`` is replaced verbatim (covering short/experimental tokens that the
    generic patterns would miss); known GitHub token shapes are replaced too.
    """
    if not text:
        return ""
    safe = str(text)
    if token:
        safe = safe.replace(token, REDACTED)
        # A token may show up URL-encoded or base64 encoded inside git/gh errors.
        try:
            import base64

            for encoded in (
                base64.b64encode(f"x-access-token:{token}".encode()).decode(),
                base64.b64encode(token.encode()).decode(),
            ):
                if encoded:
                    safe = safe.replace(encoded, REDACTED)
        except Exception:  # pragma: no cover - defensive only
            pass
    for pattern in _TOKEN_PATTERNS:
        safe = pattern.sub(REDACTED, safe)
    return safe


def resolve_token(environ: Optional[Mapping[str, str]] = None, env_var: str = TOKEN_ENV_VAR) -> str:
    """Read the cleanup token from the environment, or raise.

    The value is never echoed back, including in the exception message.
    """
    env = os.environ if environ is None else environ
    raw = (env.get(env_var) or "").strip()
    if not raw:
        raise CleanupTokenError(
            f"{env_var} is not set. The central cleanup repository must expose the "
            f"dedicated cleanup token as a repository secret named {env_var} and map it "
            "to an environment variable of the same name."
        )
    if any(ch.isspace() for ch in raw):
        raise CleanupTokenError(f"{env_var} contains whitespace and cannot be a valid token.")
    return raw


def build_child_env(
    token: str,
    *,
    base: Optional[Mapping[str, str]] = None,
    extra: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """Environment for ``gh``/``git`` children using *only* the cleanup token."""
    env = {k: v for k, v in (os.environ if base is None else base).items() if k not in FORBIDDEN_ENV_VARS}
    env[TOKEN_ENV_VAR] = token
    env["GH_TOKEN"] = token
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GCM_INTERACTIVE", "never")
    env.setdefault("GH_PROMPT_DISABLED", "1")
    if extra:
        env.update({k: v for k, v in extra.items()})
    return env


@dataclass(frozen=True)
class ApiResult:
    """Result of a single API call.

    ``ok`` is False for any transport/permission failure. ``status`` carries the
    HTTP status when it can be recovered from the CLI output.
    """

    ok: bool
    status: Optional[int] = None
    data: Any = None
    error: Optional[str] = None

    @property
    def forbidden(self) -> bool:
        return self.status in (401, 403)

    @property
    def not_found(self) -> bool:
        return self.status == 404


def _status_from_output(stderr: str) -> Optional[int]:
    match = _HTTP_STATUS_RE.search(stderr or "")
    if match:
        try:
            return int(match.group(1))
        except ValueError:  # pragma: no cover - defensive only
            return None
    lowered = (stderr or "").lower()
    if "not found" in lowered:
        return 404
    if "forbidden" in lowered or "resource not accessible" in lowered:
        return 403
    if "rate limit" in lowered:
        return 403
    if "unauthorized" in lowered or "bad credentials" in lowered:
        return 401
    return None


class GhCliClient:
    """Thin, redacting wrapper around ``gh api``.

    Only the endpoints the cleanup system needs are exposed, so the blast radius
    of a bug is limited to those calls.
    """

    def __init__(
        self,
        token: Optional[str] = None,
        *,
        token_env: str = TOKEN_ENV_VAR,
        timeout: int = 60,
        max_retries: int = 3,
        runner: Any = None,
        sleep: Any = None,
        log: Any = None,
    ) -> None:
        self._token = token
        self.token_env = token_env
        self.timeout = timeout
        self.max_retries = max_retries
        self._runner = runner or self._default_runner
        self._sleep = sleep or time.sleep
        self._log = log or (lambda msg: print(msg, file=sys.stderr))

    # -- token -----------------------------------------------------------
    @property
    def token(self) -> str:
        if self._token is None:
            self._token = resolve_token(env_var=self.token_env)
        return self._token

    def child_env(self, extra: Optional[Mapping[str, str]] = None) -> dict[str, str]:
        return build_child_env(self.token, extra=extra)

    # -- transport -------------------------------------------------------
    def _default_runner(self, cmd: list[str], env: dict[str, str], timeout: int):
        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=env,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:  # pragma: no cover - environment specific
            raise GhUnavailableError("the `gh` CLI is required but was not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:  # pragma: no cover - environment specific
            raise GhUnavailableError(f"`gh` timed out after {timeout}s") from exc

    def api(self, endpoint: str, *, method: str = "GET") -> ApiResult:
        """Call ``gh api ENDPOINT --method METHOD`` with rate-limit retries."""
        endpoint = endpoint.lstrip("/")
        cmd = ["gh", "api", endpoint, "--method", method]
        last_error = "unknown error"
        last_status: Optional[int] = None

        for attempt in range(self.max_retries + 1):
            proc = self._runner(cmd, self.child_env(), self.timeout)
            stdout = getattr(proc, "stdout", "") or ""
            stderr = getattr(proc, "stderr", "") or ""
            if proc.returncode == 0:
                if not stdout.strip():
                    return ApiResult(True, 204, None, None)
                try:
                    return ApiResult(True, 200, json.loads(stdout), None)
                except json.JSONDecodeError:
                    return ApiResult(True, 200, stdout, None)

            last_status = _status_from_output(stderr)
            last_error = redact(stderr.strip() or stdout.strip() or f"gh api exited {proc.returncode}", self.token)

            retriable = last_status in (429, 500, 502, 503, 504) or (
                last_status == 403 and "rate limit" in last_error.lower()
            )
            if retriable and attempt < self.max_retries:
                wait = min(2 ** attempt, 8)
                self._log(
                    f"::warning::GitHub API rate limited or unavailable on {endpoint} "
                    f"(attempt {attempt + 1}/{self.max_retries + 1}); retrying in {wait}s"
                )
                self._sleep(wait)
                continue
            break

        return ApiResult(False, last_status, None, last_error)

    # -- endpoints used by the cleanup system ----------------------------
    def get_authenticated_user(self) -> ApiResult:
        return self.api("user")

    def list_authenticated_user_repos(
        self,
        page: int = 1,
        per_page: int = 100,
        affiliation: str = "owner,collaborator,organization_member",
    ) -> ApiResult:
        """Every repository the token can see (private, owned, collaborating)."""
        return self.api(
            f"user/repos?affiliation={affiliation}&visibility=all&sort=full_name"
            f"&per_page={int(per_page)}&page={int(page)}"
        )

    def list_org_repositories(
        self, org: str, page: int = 1, per_page: int = 100, repo_type: str = "all"
    ) -> ApiResult:
        return self.api(
            f"orgs/{org}/repos?type={repo_type}&per_page={int(per_page)}&page={int(page)}"
        )

    def list_user_repositories(
        self, owner: str, page: int = 1, per_page: int = 100, repo_type: str = "owner"
    ) -> ApiResult:
        return self.api(
            f"users/{owner}/repos?type={repo_type}&per_page={int(per_page)}&page={int(page)}"
        )

    def get_repository(self, repo: str) -> ApiResult:
        return self.api(f"repos/{repo}")

    def list_workflow_runs(self, repo: str, page: int = 1, per_page: int = 100) -> ApiResult:
        return self.api(f"repos/{repo}/actions/runs?per_page={int(per_page)}&page={int(page)}")

    def delete_workflow_run(self, repo: str, run_id: int) -> ApiResult:
        result = self.api(f"repos/{repo}/actions/runs/{int(run_id)}", method="DELETE")
        if result.ok:
            return result
        # A 404 on DELETE means the run is already gone: the desired end state.
        if result.not_found:
            return ApiResult(True, 404, None, None)
        return result

    def list_open_pulls(self, repo: str, page: int = 1, per_page: int = 1) -> ApiResult:
        return self.api(f"repos/{repo}/pulls?state=open&per_page={int(per_page)}&page={int(page)}")


def install_gh_transport(client: GhCliClient) -> None:  # pragma: no cover - convenience hook
    """No-op placeholder kept for symmetry; transport is selected by injection."""
    return None


def summarise_endpoints(endpoints: Iterable[str]) -> str:  # pragma: no cover - diagnostics helper
    return ", ".join(endpoints)

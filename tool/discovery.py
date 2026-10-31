#!/usr/bin/env python3
"""Automatic discovery of every repository the cleanup token can manage.

Discovery runs at every execution; no hand-maintained repository list is
required. Sources are queried with full pagination and merged, then
de-duplicated case-insensitively (GitHub treats repository names that way):

1. ``GET /user/repos`` with ``affiliation=owner,collaborator,organization_member``
   and ``visibility=all`` — every repository the token can see, including
   private ones and organisation repositories.
2. For each account in ``config/accounts.txt`` that is not the authenticated
   user: ``GET /orgs/{account}/repos?type=all`` (organisation) with a fallback to
   ``GET /users/{account}/repos?type=owner`` (user account / public only).

The token decides what is discoverable. A repository visible in the GitHub
dashboard is **not** necessarily writable by the token: the per-repository
``permissions`` object is recorded and the orchestrator never mutates a
repository without write access.

A fine-grained personal access token restricted to *selected repositories*
silently hides the others. :func:`discover_repositories` compares the public
repositories of the authenticated user with the discovery result and reports
the difference, so a restricted token is visible in the report instead of
silently shrinking the run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from tool.repositories import RepositoryConfigError, is_valid_owner, resolve_config_path

DEFAULT_ACCOUNTS_RELATIVE_PATH = "config/accounts.txt"
MAX_ACCOUNTS = 100
MAX_CONFIG_BYTES = 262_144
PER_PAGE = 100
MAX_PAGES = 100  # 10 000 repositories per source

SOURCE_AUTHENTICATED_USER = "authenticated-user"
SOURCE_PREFIX_ORG = "org:"
SOURCE_PREFIX_USER = "user:"


class DiscoveryError(RuntimeError):
    """Raised when discovery cannot produce a trustworthy result at all."""


@dataclass(frozen=True)
class Account:
    """A GitHub account whose repositories should be discovered."""

    login: str
    kind: str = "auto"  # auto | user | org

    def as_dict(self) -> dict:
        return {"login": self.login, "kind": self.kind}


@dataclass(frozen=True)
class AccountList:
    accounts: tuple[Account, ...] = ()
    errors: tuple[str, ...] = ()
    duplicates: tuple[str, ...] = ()
    source: str = ""

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "accounts": [account.as_dict() for account in self.accounts],
            "errors": list(self.errors),
            "duplicates": list(self.duplicates),
        }


def parse_accounts(text: str, *, source: str = "<memory>") -> AccountList:
    """Parse ``config/accounts.txt`` (one login per line, ``#`` comments)."""
    accounts: list[Account] = []
    errors: list[str] = []
    duplicates: list[str] = []
    seen: set[str] = set()

    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        kind = "auto"
        if ":" in line:
            prefix, _, rest = line.partition(":")
            if prefix.strip().lower() in ("user", "org"):
                kind = prefix.strip().lower()
                line = rest.strip()
        if not is_valid_owner(line):
            errors.append(
                f"line {number}: invalid account {line!r} "
                "(expected a GitHub login: letters, digits and hyphens only)"
            )
            continue
        key = line.lower()
        if key in seen:
            duplicates.append(line)
            continue
        if len(accounts) >= MAX_ACCOUNTS:
            errors.append(f"line {number}: more than {MAX_ACCOUNTS} accounts configured")
            break
        seen.add(key)
        accounts.append(Account(login=line, kind=kind))

    return AccountList(
        accounts=tuple(accounts),
        errors=tuple(errors),
        duplicates=tuple(duplicates),
        source=source,
    )


def load_accounts(
    path: str | Path = DEFAULT_ACCOUNTS_RELATIVE_PATH,
    *,
    repo_root: str | Path = ".",
) -> AccountList:
    resolved = resolve_config_path(path, repo_root=repo_root)
    if not resolved.exists():
        raise RepositoryConfigError(f"accounts file not found: {resolved}")
    if resolved.stat().st_size > MAX_CONFIG_BYTES:
        raise RepositoryConfigError(f"accounts file is too large: {resolved}")
    try:
        text = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise RepositoryConfigError(f"accounts file is not valid UTF-8: {resolved}") from exc
    return parse_accounts(text, source=str(resolved))


@dataclass(frozen=True)
class DiscoveredRepository:
    """One repository found by discovery, with everything the report needs."""

    full_name: str
    owner_login: str
    owner_type: str  # "User" | "Organization"
    default_branch: str = ""
    private: bool = False
    archived: bool = False
    fork: bool = False
    disabled: bool = False
    permissions: dict[str, bool] = field(default_factory=dict)
    sources: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return self.full_name.lower()

    @property
    def can_write(self) -> bool:
        return bool(
            self.permissions.get("push")
            or self.permissions.get("admin")
            or self.permissions.get("maintain")
        )

    @property
    def visibility(self) -> str:
        return "private" if self.private else "public"

    def as_dict(self) -> dict:
        return {
            "full_name": self.full_name,
            "owner_login": self.owner_login,
            "owner_type": self.owner_type,
            "default_branch": self.default_branch,
            "private": self.private,
            "visibility": self.visibility,
            "archived": self.archived,
            "fork": self.fork,
            "disabled": self.disabled,
            "permissions": dict(self.permissions),
            "can_write": self.can_write,
            "sources": list(self.sources),
        }

    @classmethod
    def from_api(cls, payload: Any, *, source: str) -> Optional["DiscoveredRepository"]:
        if not isinstance(payload, dict):
            return None
        full_name = payload.get("full_name")
        owner = payload.get("owner") if isinstance(payload.get("owner"), dict) else {}
        if not isinstance(full_name, str) or "/" not in full_name or not owner:
            return None
        permissions = payload.get("permissions")
        return cls(
            full_name=full_name,
            owner_login=str(owner.get("login") or full_name.split("/", 1)[0]),
            owner_type=str(owner.get("type") or "User"),
            default_branch=str(payload.get("default_branch") or ""),
            private=bool(payload.get("private")),
            archived=bool(payload.get("archived")),
            fork=bool(payload.get("fork")),
            disabled=bool(payload.get("disabled")),
            permissions={str(k): bool(v) for k, v in (permissions or {}).items()},
            sources=(source,),
        )


@dataclass
class DiscoverySource:
    name: str
    endpoint: str
    kind: str = ""  # authenticated-user | org | user
    repositories: int = 0
    pages_read: int = 0
    complete: bool = True
    skipped: bool = False
    error: str = ""
    status: Optional[int] = None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "endpoint": self.endpoint,
            "kind": self.kind,
            "repositories": self.repositories,
            "pages_read": self.pages_read,
            "complete": self.complete,
            "skipped": self.skipped,
            "error": self.error,
            "status": self.status,
        }


@dataclass
class DiscoveryResult:
    repositories: list[DiscoveredRepository] = field(default_factory=list)
    sources: list[DiscoverySource] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    authenticated_user: str = ""
    account_kinds: dict[str, str] = field(default_factory=dict)
    scoped_token_missing: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.errors and all(source.complete for source in self.sources)

    @property
    def count(self) -> int:
        return len(self.repositories)

    def by_owner(self, owner: str) -> list[DiscoveredRepository]:
        return [repo for repo in self.repositories if repo.owner_login.lower() == owner.lower()]

    def get(self, full_name: str) -> Optional[DiscoveredRepository]:
        wanted = full_name.lower()
        for repo in self.repositories:
            if repo.key == wanted:
                return repo
        return None

    def as_dict(self) -> dict:
        return {
            "authenticated_user": self.authenticated_user,
            "complete": self.complete,
            "count": self.count,
            "accounts": dict(self.account_kinds),
            "sources": [source.as_dict() for source in self.sources],
            "errors": list(self.errors),
            "notes": list(self.notes),
            "scoped_token_missing": list(self.scoped_token_missing),
            "repositories": [repo.as_dict() for repo in self.repositories],
        }


def _paginate(
    fetch_page: Any,
    *,
    name: str,
    endpoint: str,
    kind: str,
    per_page: int,
    max_pages: int,
) -> tuple[list[dict], DiscoverySource]:
    """Read every page of a listing endpoint; fail closed on any error."""
    payloads: list[dict] = []
    source = DiscoverySource(name=name, endpoint=endpoint, kind=kind)
    page = 1
    while page <= max_pages:
        result = fetch_page(page)
        if not result.ok:
            source.complete = False
            source.status = result.status
            source.error = f"page {page} failed (status {result.status}): {result.error}"
            return payloads, source
        data = result.data
        if not isinstance(data, list):
            source.complete = False
            source.error = f"page {page} did not return a list"
            return payloads, source
        source.pages_read = page
        for entry in data:
            if isinstance(entry, dict):
                payloads.append(entry)
        if len(data) < per_page:
            break
        page += 1
    else:
        source.complete = False
        source.error = f"stopped after {max_pages} pages while results were still returned"
    return payloads, source


def discover_repositories(
    client: Any,
    accounts: Sequence[Account] = (),
    *,
    include_authenticated_user: bool = True,
    per_page: int = PER_PAGE,
    max_pages: int = MAX_PAGES,
) -> DiscoveryResult:
    """Discover every repository across all sources and de-duplicate them."""
    result = DiscoveryResult()

    authenticated = ""
    user_result = client.get_authenticated_user()
    if user_result.ok and isinstance(user_result.data, dict):
        authenticated = str(user_result.data.get("login") or "")
    result.authenticated_user = authenticated

    collected: dict[str, DiscoveredRepository] = {}

    def add(payload: dict, source_name: str) -> None:
        repo = DiscoveredRepository.from_api(payload, source=source_name)
        if repo is None:
            result.errors.append(f"unparseable repository entry from {source_name}")
            return
        existing = collected.get(repo.key)
        if existing is None:
            collected[repo.key] = repo
            return
        merged_sources = tuple(dict.fromkeys(existing.sources + repo.sources))
        collected[repo.key] = DiscoveredRepository(
            full_name=existing.full_name,
            owner_login=existing.owner_login,
            owner_type=existing.owner_type,
            default_branch=existing.default_branch or repo.default_branch,
            private=existing.private or repo.private,
            archived=existing.archived or repo.archived,
            fork=existing.fork or repo.fork,
            disabled=existing.disabled or repo.disabled,
            permissions=existing.permissions or repo.permissions,
            sources=merged_sources,
        )

    # 1. everything the token can see
    if include_authenticated_user:
        payloads, source = _paginate(
            lambda page: client.list_authenticated_user_repos(page=page, per_page=per_page),
            name=SOURCE_AUTHENTICATED_USER,
            endpoint="GET /user/repos",
            kind="authenticated-user",
            per_page=per_page,
            max_pages=max_pages,
        )
        for payload in payloads:
            add(payload, SOURCE_AUTHENTICATED_USER)
        source.repositories = len(payloads)
        result.sources.append(source)
        if not source.complete:
            result.errors.append(f"{SOURCE_AUTHENTICATED_USER}: {source.error}")

    # 2. explicitly configured accounts (organisations first, then user accounts)
    configured = {account.login.lower() for account in accounts}
    for account in accounts:
        if authenticated and account.login.lower() == authenticated.lower():
            result.account_kinds[account.login] = "self"
            continue

        name = f"{SOURCE_PREFIX_ORG}{account.login}"
        payloads, source = _paginate(
            lambda page, login=account.login: client.list_org_repositories(
                login, page=page, per_page=per_page
            ),
            name=name,
            endpoint=f"GET /orgs/{account.login}/repos",
            kind="org",
            per_page=per_page,
            max_pages=max_pages,
        )
        if source.error and source.pages_read == 0:
            # not an organisation (or no organisation access): fall back to the
            # public repository listing of a user account.
            name = f"{SOURCE_PREFIX_USER}{account.login}"
            payloads, source = _paginate(
                lambda page, login=account.login: client.list_user_repositories(
                    login, page=page, per_page=per_page
                ),
                name=name,
                endpoint=f"GET /users/{account.login}/repos",
                kind="user",
                per_page=per_page,
                max_pages=max_pages,
            )
            result.account_kinds[account.login] = "user"
            if source.error and source.pages_read == 0 and source.status in (403, 404):
                source.skipped = True
                source.complete = True
                result.notes.append(
                    f"account {account.login!r} is neither a readable organisation nor a readable "
                    "user account (HTTP "
                    f"{source.status}); it contributed no repositories"
                )
        else:
            result.account_kinds[account.login] = "org"

        for payload in payloads:
            add(payload, name)
        source.repositories = len(payloads)
        result.sources.append(source)
        if not source.complete and not source.skipped:
            result.errors.append(f"{name}: {source.error}")

    # 3. diagnostic: a fine-grained token restricted to selected repositories
    if authenticated and include_authenticated_user:
        public_payloads, _source = _paginate(
            lambda page: client.list_user_repositories(
                authenticated, page=page, per_page=per_page, repo_type="owner"
            ),
            name=f"{SOURCE_PREFIX_USER}{authenticated}",
            endpoint=f"GET /users/{authenticated}/repos",
            kind="user",
            per_page=per_page,
            max_pages=max_pages,
        )
        missing = sorted(
            {
                str(payload.get("full_name"))
                for payload in public_payloads
                if isinstance(payload.get("full_name"), str)
                and payload.get("full_name", "").lower() not in collected
            }
        )
        if missing:
            result.scoped_token_missing = missing
            result.notes.append(
                "The token cannot see "
                f"{len(missing)} public repositories of {authenticated}. A fine-grained token "
                "limited to *selected repositories* hides all others; set its repository access "
                "to 'All repositories' for every account you want cleaned."
            )

    result.repositories = sorted(collected.values(), key=lambda repo: repo.key)
    if not result.repositories:
        result.errors.append("discovery returned no repositories")
    if configured and not any(
        source.kind in ("org", "user") and source.complete for source in result.sources
    ):
        result.notes.append(
            "No configured account could be listed as an organisation or user account; "
            "only repositories visible through /user/repos were discovered."
        )
    return result


def repositories_for_owner(
    repositories: Iterable[DiscoveredRepository], owner: str
) -> list[DiscoveredRepository]:
    wanted = owner.lower()
    return [repo for repo in repositories if repo.owner_login.lower() == wanted]

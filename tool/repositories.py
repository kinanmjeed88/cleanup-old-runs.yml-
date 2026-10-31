#!/usr/bin/env python3
"""Target repository list: parsing, validation and selection.

The list is a plain text file inside this repository (``config/repositories.txt``)
so that adding or removing a target never requires touching Python code.

Rules:

* one ``owner/name`` per line;
* blank lines and ``#`` comments are ignored;
* a malformed line is reported and skipped — it never becomes an API target;
* duplicates are removed (GitHub treats repository names case-insensitively);
* the file path is confined to the repository root, so an untrusted path or a
  path outside the checkout is rejected;
* this module never invents a repository: selection can only narrow the list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

DEFAULT_CONFIG_RELATIVE_PATH = "config/repositories.txt"
MAX_CONFIG_BYTES = 1_048_576  # 1 MiB — a repository list is never this large
MAX_REPOSITORIES = 500

# GitHub owner: alphanumeric or hyphen, no leading/trailing hyphen, max 39 chars.
_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
# GitHub repository: alphanumeric, ``-``, ``_``, ``.``; max 100 chars.
_REPO_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$")

FORBIDDEN_CHARS = ("://", "@", "\\", " ", "\t", "?", "#", "%", "*", "<", ">", "|", '"', "'")


class RepositoryConfigError(ValueError):
    """Raised when the configuration file itself is unusable."""


@dataclass(frozen=True)
class RepositoryList:
    repositories: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    duplicates: tuple[str, ...] = ()
    ignored_lines: int = 0
    source: str = ""

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "repositories": list(self.repositories),
            "errors": list(self.errors),
            "duplicates": list(self.duplicates),
            "ignored_lines": self.ignored_lines,
        }


def is_valid_owner(login: str) -> bool:
    """Strict GitHub account (user or organisation) login validation."""
    return bool(isinstance(login, str) and _OWNER_RE.match(login))


def is_valid_repository(repository: str) -> bool:
    """Strict ``owner/name`` validation (no URLs, no paths, no whitespace)."""
    if not isinstance(repository, str) or not repository:
        return False
    candidate = repository.strip()
    if candidate != repository:
        return False
    if any(token in repository for token in FORBIDDEN_CHARS):
        return False
    if repository.count("/") != 1:
        return False
    owner, name = repository.split("/", 1)
    if not owner or not name:
        return False
    if not _OWNER_RE.match(owner):
        return False
    if not _REPO_RE.match(name):
        return False
    if name in (".", ".."):
        return False
    return True


def normalise(repository: str) -> str:
    return repository.strip()


def parse_repositories(text: str, *, source: str = "<memory>") -> RepositoryList:
    """Parse and validate the repository list."""
    repositories: list[str] = []
    errors: list[str] = []
    duplicates: list[str] = []
    ignored = 0
    seen: set[str] = set()

    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            ignored += 1
            continue
        if not is_valid_repository(line):
            errors.append(
                f"line {number}: invalid repository {line!r} "
                "(expected `owner/name` with no scheme, path or whitespace)"
            )
            continue
        key = line.lower()
        if key in seen:
            duplicates.append(line)
            continue
        if len(repositories) >= MAX_REPOSITORIES:
            errors.append(
                f"line {number}: more than {MAX_REPOSITORIES} repositories configured; "
                "remaining entries ignored"
            )
            break
        seen.add(key)
        repositories.append(line)

    return RepositoryList(
        repositories=tuple(repositories),
        errors=tuple(errors),
        duplicates=tuple(duplicates),
        ignored_lines=ignored,
        source=source,
    )


def resolve_config_path(path: str | Path, *, repo_root: str | Path) -> Path:
    """Resolve the config path, rejecting anything outside the repository root."""
    root = Path(repo_root).resolve()
    candidate = Path(path)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RepositoryConfigError(
            f"repository list {resolved} is outside the repository root {root}; "
            "refusing to read target repositories from an untrusted location"
        ) from exc
    return resolved


def load_repository_config(
    path: str | Path = DEFAULT_CONFIG_RELATIVE_PATH,
    *,
    repo_root: str | Path = ".",
) -> RepositoryList:
    """Read and parse the repository list from inside the repository root."""
    resolved = resolve_config_path(path, repo_root=repo_root)
    if not resolved.exists():
        raise RepositoryConfigError(f"repository list not found: {resolved}")
    if not resolved.is_file():
        raise RepositoryConfigError(f"repository list is not a regular file: {resolved}")
    size = resolved.stat().st_size
    if size > MAX_CONFIG_BYTES:
        raise RepositoryConfigError(f"repository list is {size} bytes, larger than the {MAX_CONFIG_BYTES} byte limit")
    try:
        text = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise RepositoryConfigError(f"repository list is not valid UTF-8: {resolved}") from exc
    return parse_repositories(text, source=str(resolved))


def select_repositories(
    repositories: Iterable[str],
    selector: Optional[str] = None,
) -> tuple[str, ...]:
    """Narrow the configured list to ``selector`` (``all`` or one configured repo).

    A repository that is not in the configuration is rejected — the workflow
    input can never widen the target set.
    """
    configured = tuple(repositories)
    if selector is None:
        return configured
    value = selector.strip()
    if value == "" or value.lower() == "all":
        return configured
    if not is_valid_repository(value):
        raise RepositoryConfigError(f"invalid repository selector {selector!r}; expected `owner/name` or `all`")
    for repository in configured:
        if repository.lower() == value.lower():
            return (repository,)
    raise RepositoryConfigError(
        f"repository {value!r} is not listed in the configuration; "
        "add it to the repository list before targeting it explicitly"
    )


@dataclass
class Selection:
    repositories: tuple[str, ...] = ()
    errors: list[str] = field(default_factory=list)
    duplicates: tuple[str, ...] = ()
    source: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors

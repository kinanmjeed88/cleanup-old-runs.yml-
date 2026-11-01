"""Discovery tests: pagination, multi-source merging, de-duplication, failure modes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.fakes import FakeBranchRunner, FakeClient, FakeWorkspaceFactory, build_runs
from tool import central_cleanup
from tool.central_cleanup import EXIT_CONFIG, EXIT_FAILURES, EXIT_OK, main, select_targets
from tool.discovery import (
    MAX_ACCOUNTS,
    Account,
    discover_repositories,
    load_accounts,
    parse_accounts,
)
from tool.github_client import ApiResult
from tool.repositories import RepositoryConfigError


# ---------------------------------------------------------------------------
# accounts file
# ---------------------------------------------------------------------------


def test_parse_accounts_supports_prefixes_comments_and_blank_lines():
    parsed = parse_accounts(
        """
        # owners to clean
        kinanmjeed88
        org:techtouchAI   # an organisation
        user:someone-else

        kinanmjeed88
        """
    )

    assert [account.login for account in parsed.accounts] == [
        "kinanmjeed88",
        "techtouchAI",
        "someone-else",
    ]
    assert [account.kind for account in parsed.accounts] == ["auto", "org", "user"]
    assert parsed.duplicates == ("kinanmjeed88",)
    assert parsed.errors == ()


@pytest.mark.parametrize("line", ["not valid", "user:", "bad/name", "-leading-hyphen"])
def test_parse_accounts_rejects_invalid_logins(line):
    parsed = parse_accounts(line)
    assert parsed.accounts == ()
    assert parsed.errors and "invalid account" in parsed.errors[0]


def test_parse_accounts_caps_the_number_of_accounts():
    text = "\n".join(f"user{index}" for index in range(MAX_ACCOUNTS + 3))
    parsed = parse_accounts(text)
    assert len(parsed.accounts) == MAX_ACCOUNTS
    assert any("more than" in error for error in parsed.errors)


def test_load_accounts_refuses_a_missing_file(tmp_path: Path):
    with pytest.raises(RepositoryConfigError):
        load_accounts("config/accounts.txt", repo_root=tmp_path)


def test_repository_accounts_file_is_loadable():
    parsed = load_accounts("config/accounts.txt", repo_root=Path(__file__).resolve().parents[1])
    logins = {account.login.lower() for account in parsed.accounts}
    assert {"kinanmjeed88", "techtouchai"} <= logins
    assert not parsed.errors


# ---------------------------------------------------------------------------
# discovery engine
# ---------------------------------------------------------------------------


def test_discovery_reads_every_page():
    client = FakeClient()
    client.authenticated_login = "acme"
    for index in range(7):
        client.add_repository(f"acme/repo-{index}", runs=[])

    result = discover_repositories(client, (), per_page=2)

    assert result.count == 7
    assert result.complete is True
    source = result.sources[0]
    assert source.pages_read == 4  # 2 + 2 + 2 + 1
    assert source.repositories == 7


def test_discovery_merges_sources_without_duplicates():
    class CollaboratorClient(FakeClient):
        """/user/repos also returns repositories of the organisations I belong to."""

        def list_authenticated_user_repos(self, page=1, per_page=100, affiliation=""):
            repos = sorted(self.repositories)
            return self._page(repos, page, per_page, include_permissions=True)

    client = CollaboratorClient()
    client.authenticated_login = "maintainer"
    client.accounts["maintainer"] = "User"
    client.add_repository("acme/shared", runs=[], owner_type="Organization")
    client.add_repository("acme/only-org", runs=[], owner_type="Organization")
    client.add_repository("maintainer/mine", runs=[])

    result = discover_repositories(client, (Account("acme"),))

    assert result.count == 3
    shared = result.get("acme/shared")
    assert shared is not None
    assert set(shared.sources) == {"authenticated-user", "org:acme"}
    assert result.account_kinds["acme"] == "org"


def test_a_user_account_falls_back_to_the_public_listing():
    """`techtouchAI` is a User, so `/orgs/.../repos` 404s and `/users/...` answers."""
    client = FakeClient()
    client.authenticated_login = "acme"
    client.add_repository("acme/one", runs=[])
    client.add_repository("techtouchai/alpha", runs=[], can_push=False)

    result = discover_repositories(client, (Account("techtouchai"),))

    assert result.account_kinds["techtouchai"] == "user"
    assert result.get("techtouchai/alpha") is not None
    repo = result.get("techtouchai/alpha")
    assert repo is not None and repo.can_write is False  # read-only, but discovered
    assert result.complete is True
    endpoints = {source.endpoint for source in result.sources}
    assert "GET /users/techtouchai/repos" in endpoints


def test_an_unreachable_account_is_reported_and_does_not_bridge_discovery():
    client = FakeClient()
    client.authenticated_login = "acme"
    client.add_repository("acme/one", runs=[])
    client.fail_accounts.add("ghost")

    result = discover_repositories(client, (Account("ghost"),))

    assert result.get("acme/one") is not None
    assert any(source.skipped for source in result.sources)
    assert any("ghost" in note for note in result.notes)
    assert result.complete is True


def test_a_broken_page_makes_discovery_incomplete():
    class BrokenClient(FakeClient):
        def list_authenticated_user_repos(self, page=1, per_page=100, affiliation=""):
            if page == 2:
                return ApiResult(False, 500, None, "gh: Server Error (HTTP 500)")
            return super().list_authenticated_user_repos(page=page, per_page=per_page)

    client = BrokenClient()
    client.authenticated_login = "acme"
    for index in range(5):
        client.add_repository(f"acme/repo-{index}", runs=[])

    result = discover_repositories(client, (), per_page=2)

    assert result.complete is False
    assert result.errors
    assert result.count == 2  # only page 1 was trusted


def test_a_scoped_token_is_diagnosed():
    """Repos that exist but are invisible to the token must be reported, not hidden."""

    class ScopedClient(FakeClient):
        def list_authenticated_user_repos(self, page=1, per_page=100, affiliation=""):
            # A fine-grained token limited to selected repositories.
            return ApiResult(True, 200, [], None)

    client = ScopedClient()
    client.authenticated_login = "acme"
    client.accounts["acme"] = "User"
    client.repositories["acme/hidden"] = {
        "full_name": "acme/hidden",
        "default_branch": "main",
        "owner": {"login": "acme", "type": "User"},
    }

    result = discover_repositories(client, ())

    assert result.count == 0
    assert result.scoped_token_missing == ["acme/hidden"]
    assert any("selected repositories" in note for note in result.notes)


def test_discovery_never_returns_the_same_repository_twice():
    client = FakeClient()
    client.authenticated_login = "acme"
    client.add_repository("acme/a", runs=[])
    client.add_repository("acme/b", runs=[])

    result = discover_repositories(client, (Account("acme"), Account("acme")))

    keys = [repo.key for repo in result.repositories]
    assert keys == sorted(set(keys))
    assert result.count == 2


# ---------------------------------------------------------------------------
# target selection
# ---------------------------------------------------------------------------


def test_select_targets_respects_modes_and_selector():
    client = FakeClient()
    client.authenticated_login = "acme"
    client.add_repository("acme/a", runs=[])
    client.add_repository("acme/b", runs=[])
    discovery = discover_repositories(client)

    assert [repo.full_name for repo in select_targets(discovery, (), discovery_mode="auto")] == [
        "acme/a",
        "acme/b",
    ]
    assert [repo.full_name for repo in select_targets(discovery, ("other/c",), discovery_mode="list")] == [
        "other/c"
    ]
    assert [
        repo.full_name for repo in select_targets(discovery, ("other/c",), discovery_mode="union")
    ] == ["acme/a", "acme/b", "other/c"]
    assert [
        repo.full_name
        for repo in select_targets(discovery, (), discovery_mode="auto", selector="acme/b")
    ] == ["acme/b"]
    assert len(select_targets(discovery, (), discovery_mode="auto", limit=1)) == 1


def test_select_targets_refuses_an_undiscovered_repository():
    client = FakeClient()
    client.authenticated_login = "acme"
    client.add_repository("acme/a", runs=[])
    discovery = discover_repositories(client)

    with pytest.raises(RepositoryConfigError):
        select_targets(discovery, (), discovery_mode="auto", selector="evil/repo")


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "repositories.txt").write_text("acme/a\n", encoding="utf-8")
    (tmp_path / "config" / "accounts.txt").write_text("acme\n", encoding="utf-8")
    (tmp_path / "tool").mkdir()
    (tmp_path / "tool" / "branch_cleanup.py").write_text("# stub\n", encoding="utf-8")
    return tmp_path


def args_for(root: Path, *extra: str) -> list[str]:
    return [
        "--repo-root",
        str(root),
        "--reports-dir",
        str(root / "diagnostics"),
        *extra,
    ]


def wire(monkeypatch, root: Path, client: FakeClient, runner=None) -> None:
    monkeypatch.setattr(central_cleanup, "GhCliClient", lambda **kwargs: client)
    monkeypatch.setattr(
        central_cleanup, "GitWorkspaceFactory", lambda **kwargs: FakeWorkspaceFactory(root / "ws")
    )
    monkeypatch.setattr(
        central_cleanup, "BranchCleanupRunner", lambda **kwargs: runner or FakeBranchRunner()
    )


def test_cli_discover_only_writes_a_discovery_report(root: Path, monkeypatch, capsys):
    monkeypatch.setenv("CLEANUP_TOKEN", "ghp_discoverytoken1234567890")
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    client = FakeClient()
    client.authenticated_login = "acme"
    client.add_repository("acme/a", runs=build_runs(5))
    client.add_repository("acme/b", runs=build_runs(5), can_push=False)
    wire(monkeypatch, root, client)

    assert main(args_for(root, "--discover-only")) == EXIT_OK

    summary = json.loads((root / "diagnostics" / "summary.json").read_text(encoding="utf-8"))
    discovery = summary["discovery"]
    assert discovery["count"] == 2
    assert discovery["complete"] is True
    assert {repo["full_name"] for repo in discovery["repositories"]} == {"acme/a", "acme/b"}
    # nothing was touched
    assert client.delete_calls == []
    out = capsys.readouterr().out
    assert "DISCOVERED REPOSITORIES: 2" in out


def test_cli_auto_mode_discovers_without_a_repository_list(root: Path, monkeypatch):
    (root / "config" / "repositories.txt").unlink()
    monkeypatch.setenv("CLEANUP_TOKEN", "ghp_discoverytoken1234567890")
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    client = FakeClient()
    client.authenticated_login = "acme"
    client.add_repository("acme/a", runs=build_runs(25))
    client.add_repository("acme/b", runs=build_runs(8))
    wire(monkeypatch, root, client)

    assert main(args_for(root)) == EXIT_OK

    summary = json.loads((root / "diagnostics" / "summary.json").read_text(encoding="utf-8"))
    assert [item["repository"] for item in summary["repositories"]] == ["acme/a", "acme/b"]
    assert summary["totals"]["runs_planned_deletions"] == 5


def test_cli_processes_read_only_repositories_as_skipped(root: Path, monkeypatch):
    monkeypatch.setenv("CLEANUP_TOKEN", "ghp_discoverytoken1234567890")
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    client = FakeClient()
    client.authenticated_login = "acme"
    client.add_repository("acme/mine", runs=build_runs(30))
    client.add_repository("techtouchai/theirs", runs=build_runs(30), can_push=False)
    client.accounts["techtouchai"] = "User"
    (root / "config" / "accounts.txt").write_text("acme\ntechtouchai\n", encoding="utf-8")
    wire(monkeypatch, root, client)

    assert main(args_for(root)) == EXIT_OK

    summary = json.loads((root / "diagnostics" / "summary.json").read_text(encoding="utf-8"))
    statuses = {item["repository"]: item["status"] for item in summary["repositories"]}
    assert statuses == {"acme/mine": "DRY RUN", "techtouchai/theirs": "SKIPPED"}
    assert client.delete_calls == []


def test_cli_live_mode_is_blocked_when_discovery_is_incomplete(root: Path, monkeypatch):
    class BrokenClient(FakeClient):
        def list_authenticated_user_repos(self, page=1, per_page=100, affiliation=""):
            return ApiResult(False, 500, None, "gh: Server Error (HTTP 500)")

    monkeypatch.setenv("CLEANUP_TOKEN", "ghp_discoverytoken1234567890")
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    client = BrokenClient()
    client.authenticated_login = "acme"
    client.add_repository("acme/a", runs=build_runs(30))
    wire(monkeypatch, root, client)

    assert main(args_for(root, "--mode", "live", "--confirm-live", "DELETE")) == EXIT_CONFIG
    assert client.delete_calls == []


def test_cli_never_processes_a_repository_it_did_not_discover(root: Path, monkeypatch):
    monkeypatch.setenv("CLEANUP_TOKEN", "ghp_discoverytoken1234567890")
    client = FakeClient()
    client.authenticated_login = "acme"
    client.add_repository("acme/a", runs=build_runs(5))
    wire(monkeypatch, root, client)

    assert main(args_for(root, "--repository", "someone/else")) == EXIT_CONFIG
    assert client.delete_calls == []


def test_cli_fails_when_discovery_finds_nothing(root: Path, monkeypatch):
    monkeypatch.setenv("CLEANUP_TOKEN", "ghp_discoverytoken1234567890")
    client = FakeClient()
    client.authenticated_login = "acme"
    wire(monkeypatch, root, client)

    assert main(args_for(root)) == EXIT_CONFIG
    assert client.delete_calls == []


def test_cli_limit_caps_the_number_of_repositories(root: Path, monkeypatch):
    monkeypatch.setenv("CLEANUP_TOKEN", "ghp_discoverytoken1234567890")
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    client = FakeClient()
    client.authenticated_login = "acme"
    for name in ("a", "b", "c"):
        client.add_repository(f"acme/{name}", runs=build_runs(10))
    wire(monkeypatch, root, client)

    assert main(args_for(root, "--limit", "2")) == EXIT_OK
    summary = json.loads((root / "diagnostics" / "summary.json").read_text(encoding="utf-8"))
    assert len(summary["repositories"]) == 2


def test_cli_reports_a_failing_repository_and_continues(root: Path, monkeypatch):
    monkeypatch.setenv("CLEANUP_TOKEN", "ghp_discoverytoken1234567890")
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    client = FakeClient()
    client.authenticated_login = "acme"
    client.add_repository("acme/a", runs=build_runs(30))
    client.add_repository("acme/b", runs=build_runs(30))
    client.forbid_runs.add("acme/a")
    wire(monkeypatch, root, client)

    assert main(args_for(root)) == EXIT_FAILURES
    summary = json.loads((root / "diagnostics" / "summary.json").read_text(encoding="utf-8"))
    statuses = {item["repository"]: item["status"] for item in summary["repositories"]}
    assert statuses == {"acme/a": "FAILED", "acme/b": "DRY RUN"}

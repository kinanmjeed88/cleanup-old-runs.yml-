"""Repository list parsing, validation and selection."""

from __future__ import annotations

from pathlib import Path

import pytest

from tool.repositories import (
    RepositoryConfigError,
    is_valid_repository,
    load_repository_config,
    parse_repositories,
    resolve_config_path,
    select_repositories,
)


def test_parses_simple_list():
    parsed = parse_repositories("owner1/repo1\nowner2/repo2\n")
    assert parsed.repositories == ("owner1/repo1", "owner2/repo2")
    assert parsed.errors == ()


def test_ignores_blank_lines_and_comments():
    text = """
    # a comment
    owner1/repo1   # inline comment

    # another comment
    owner2/repo2
    """
    parsed = parse_repositories(text)
    assert parsed.repositories == ("owner1/repo1", "owner2/repo2")
    assert parsed.ignored_lines >= 3


def test_rejects_invalid_entries():
    text = "owner1/repo1\nnot-a-repo\nhttps://github.com/owner/x.git\nowner/\n/name\nowner/name/extra\n"
    parsed = parse_repositories(text)
    assert parsed.repositories == ("owner1/repo1",)
    assert len(parsed.errors) == 5
    assert all("invalid repository" in error for error in parsed.errors)


def test_duplicates_are_removed_case_insensitively():
    parsed = parse_repositories("Owner/Repo\nowner/repo\nOWNER/REPO\n")
    assert parsed.repositories == ("Owner/Repo",)
    assert len(parsed.duplicates) == 2


@pytest.mark.parametrize(
    "value",
    [
        "owner/repo",
        "Owner-1/repo.name",
        "o/r",
        "my-org/my_repo-2",
    ],
)
def test_valid_names(value: str):
    assert is_valid_repository(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "owner",
        "owner/",
        "/repo",
        "owner/repo/extra",
        "https://github.com/owner/repo",
        "git@github.com:owner/repo.git",
        "owner /repo",
        "owner/repo ",
        "-owner/repo",
        "owner/.hidden",
        "owner/..",
        "owner/re po",
        "owner/repo?x=1",
    ],
)
def test_invalid_names(value: str):
    assert not is_valid_repository(value)


def test_load_from_file(tmp_path: Path):
    config = tmp_path / "config" / "repositories.txt"
    config.parent.mkdir()
    config.write_text("# comment\nowner/repo\n", encoding="utf-8")

    parsed = load_repository_config("config/repositories.txt", repo_root=tmp_path)
    assert parsed.repositories == ("owner/repo",)
    assert parsed.source == str(config.resolve())


def test_load_rejects_path_outside_repository_root(tmp_path: Path):
    outside = tmp_path.parent / "outside-repositories.txt"
    outside.write_text("owner/repo\n", encoding="utf-8")

    with pytest.raises(RepositoryConfigError, match="outside the repository root"):
        load_repository_config(outside, repo_root=tmp_path / "inside")


def test_load_rejects_traversal(tmp_path: Path):
    (tmp_path / "repo").mkdir()
    with pytest.raises(RepositoryConfigError):
        resolve_config_path("../secrets.txt", repo_root=tmp_path / "repo")


def test_load_missing_file(tmp_path: Path):
    with pytest.raises(RepositoryConfigError, match="not found"):
        load_repository_config("config/repositories.txt", repo_root=tmp_path)


def test_load_rejects_oversized_file(tmp_path: Path):
    config = tmp_path / "repositories.txt"
    config.write_text("owner/repo\n" + ("#" * 2_000_000), encoding="utf-8")
    with pytest.raises(RepositoryConfigError, match="larger than"):
        load_repository_config("repositories.txt", repo_root=tmp_path)


def test_select_all():
    parsed = parse_repositories("a/one\nb/two\n")
    assert select_repositories(parsed.repositories, "all") == ("a/one", "b/two")
    assert select_repositories(parsed.repositories, "") == ("a/one", "b/two")


def test_select_single_repository_is_case_insensitive():
    parsed = parse_repositories("a/one\nb/two\n")
    assert select_repositories(parsed.repositories, "B/Two") == ("b/two",)


def test_select_rejects_repository_not_in_config():
    parsed = parse_repositories("a/one\n")
    with pytest.raises(RepositoryConfigError, match="not listed in the configuration"):
        select_repositories(parsed.repositories, "evil/repo")


def test_select_rejects_malformed_selector():
    parsed = parse_repositories("a/one\n")
    with pytest.raises(RepositoryConfigError, match="invalid repository selector"):
        select_repositories(parsed.repositories, "https://github.com/evil/repo")


def test_configured_repository_file_is_valid(repo_root: Path):
    """The repository's own config file must always be valid."""
    parsed = load_repository_config("config/repositories.txt", repo_root=repo_root)
    assert parsed.errors == ()
    assert parsed.repositories

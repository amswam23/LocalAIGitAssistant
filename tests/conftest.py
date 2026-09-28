from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _run(args: list[str], cwd: Path) -> None:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, shell=False)
    assert result.returncode == 0, f"git {args} failed: {result.stderr.decode()}"


def make_repo(path: Path) -> Path:
    """Initialise a Git repository with a local, isolated identity."""
    path.mkdir(parents=True, exist_ok=True)
    _run(["init", "-q", "-b", "main"], path)
    _run(["config", "user.email", "tester@example.com"], path)
    _run(["config", "user.name", "Test User"], path)
    _run(["config", "commit.gpgsign", "false"], path)
    return path


def commit_file(path: Path, name: str, content: str, message: str) -> str:
    (path / name).write_text(content)
    _run(["add", "--all"], path)
    _run(["commit", "-q", "-m", message], path)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, shell=False
    )
    return result.stdout.decode().strip()


@pytest.fixture
def empty_repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "repo")


@pytest.fixture
def repo_with_commit(tmp_path: Path) -> Path:
    repo = make_repo(tmp_path / "repo")
    commit_file(repo, "README.md", "hello\n", "initial commit")
    return repo


@pytest.fixture
def bare_remote_and_clone(tmp_path: Path):
    """A bare 'remote' repository plus a clone that tracks it."""
    bare = tmp_path / "remote.git"
    bare.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True, shell=False)

    origin_work = make_repo(tmp_path / "origin_work")
    _run(["remote", "add", "origin", str(bare)], origin_work)
    commit_file(origin_work, "README.md", "hello\n", "initial commit")
    _run(["push", "-q", "-u", "origin", "main"], origin_work)

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(bare), str(clone)], check=True, shell=False
    )
    _run(["config", "user.email", "tester@example.com"], clone)
    _run(["config", "user.name", "Test User"], clone)
    _run(["config", "commit.gpgsign", "false"], clone)
    return bare, origin_work, clone

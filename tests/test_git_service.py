from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from git_local_assistant import git_service as gs  # noqa: E402

from .conftest import commit_file  # noqa: E402


def test_resolve_repo_root_on_clean_repo(repo_with_commit: Path):
    root = gs.resolve_repo_root(str(repo_with_commit))
    assert Path(root).resolve() == repo_with_commit.resolve()


def test_resolve_repo_root_from_subdirectory(repo_with_commit: Path):
    sub = repo_with_commit / "sub"
    sub.mkdir()
    root = gs.resolve_repo_root(str(sub))
    assert Path(root).resolve() == repo_with_commit.resolve()


def test_resolve_repo_root_rejects_non_git_folder(tmp_path: Path):
    not_a_repo = tmp_path / "plain_folder"
    not_a_repo.mkdir()
    with pytest.raises(gs.NotAGitRepositoryError):
        gs.resolve_repo_root(str(not_a_repo))


def test_resolve_repo_root_rejects_missing_folder(tmp_path: Path):
    with pytest.raises(gs.GitError):
        gs.resolve_repo_root(str(tmp_path / "does-not-exist"))


# -- clean / dirty detection -------------------------------------------------


def test_clean_repository_detection(repo_with_commit: Path):
    assert gs.is_clean(str(repo_with_commit)) is True
    status = gs.build_repo_status(str(repo_with_commit))
    assert status.is_clean is True
    assert status.changed_files == []


def test_modified_file_detection(repo_with_commit: Path):
    (repo_with_commit / "README.md").write_text("hello\nmore text\n")
    assert gs.is_clean(str(repo_with_commit)) is False
    files = gs.get_status_porcelain(str(repo_with_commit))
    assert len(files) == 1
    assert files[0].path == "README.md"
    assert files[0].worktree_status == "M"


def test_untracked_file_detection(repo_with_commit: Path):
    (repo_with_commit / "new_file.txt").write_text("new\n")
    files = gs.get_status_porcelain(str(repo_with_commit))
    assert any(f.is_untracked and f.path == "new_file.txt" for f in files)


def test_leading_space_preserved_in_status_code(repo_with_commit: Path):
    # An unstaged modification has a *leading* space in the two-letter code
    # (" M"). This must not be stripped away.
    (repo_with_commit / "README.md").write_text("changed\n")
    files = gs.get_status_porcelain(str(repo_with_commit))
    assert files[0].index_status == " "
    assert files[0].worktree_status == "M"
    assert files[0].code == " M"


# -- fingerprint --------------------------------------------------------------


def test_fingerprint_changes_with_file_edit(repo_with_commit: Path):
    fp1 = gs.fingerprint(str(repo_with_commit))
    (repo_with_commit / "README.md").write_text("hello\nchanged\n")
    fp2 = gs.fingerprint(str(repo_with_commit))
    assert fp1 != fp2


def test_fingerprint_changes_with_new_commit(repo_with_commit: Path):
    fp1 = gs.fingerprint(str(repo_with_commit))
    commit_file(repo_with_commit, "second.txt", "content\n", "second commit")
    fp2 = gs.fingerprint(str(repo_with_commit))
    assert fp1 != fp2


def test_fingerprint_stable_when_nothing_changes(repo_with_commit: Path):
    fp1 = gs.fingerprint(str(repo_with_commit))
    fp2 = gs.fingerprint(str(repo_with_commit))
    assert fp1 == fp2


# -- safe parsing of local commands -------------------------------------------


def test_run_local_command_handles_quoted_paths(tmp_path: Path):
    folder_with_space = tmp_path / "my folder"
    folder_with_space.mkdir()
    (folder_with_space / "file.txt").write_text("data\n")
    command = f'ls "{folder_with_space}"'
    argv = shlex.split(command)
    # shlex correctly keeps the quoted path as a single argument.
    assert argv == ["ls", str(folder_with_space)]
    result = gs.run_local_command(command, cwd=str(tmp_path))
    assert result.success is True
    assert "file.txt" in result.stdout


def test_run_local_command_uses_shell_false_argv_list(tmp_path: Path, monkeypatch):
    captured = {}
    real_run = subprocess.run

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["shell"] = kwargs.get("shell")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    gs.run_local_command("python3 -c \"print('hi')\"", cwd=str(tmp_path))
    assert captured["shell"] is False
    assert isinstance(captured["argv"], list)
    assert captured["argv"][0] == "python3"


def test_run_git_uses_shell_false(repo_with_commit: Path, monkeypatch):
    captured = {}
    real_run = subprocess.run

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["shell"] = kwargs.get("shell")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    gs.run_git(["status"], cwd=str(repo_with_commit))
    assert captured["shell"] is False
    assert captured["argv"][0] == "git"


def test_shell_metacharacters_are_inert(tmp_path: Path):
    # A semicolon here must NOT be interpreted as a command separator,
    # because no shell is ever invoked.
    marker = tmp_path / "should_not_exist.txt"
    command = f"echo hello; touch {marker}"
    result = gs.run_local_command(command, cwd=str(tmp_path))
    assert result.success is True
    assert not marker.exists()
    assert result.stdout.strip() == f"hello; touch {marker}"


# -- branches ------------------------------------------------------------------


def test_validate_branch_name_accepts_good_name():
    assert gs.validate_branch_name("feature/my-change") is None


def test_validate_branch_name_rejects_spaces():
    assert gs.validate_branch_name("bad name") is not None


def test_validate_branch_name_rejects_empty():
    assert gs.validate_branch_name("") is not None


def test_validate_branch_name_rejects_bad_characters():
    assert gs.validate_branch_name("weird~branch^name") is not None


def test_list_branches_excludes_symbolic_remote_head(bare_remote_and_clone):
    _, _, clone = bare_remote_and_clone
    branches = gs.list_branches(str(clone))
    names = [b.name for b in branches]
    assert "HEAD" not in names
    assert "origin/HEAD" not in names
    assert any(b.name == "main" and not b.is_remote for b in branches)
    assert any(b.name == "origin/main" and b.is_remote for b in branches)


# -- ahead / behind / diverged -------------------------------------------------


def test_ahead_behind_when_local_ahead(bare_remote_and_clone):
    _, _, clone = bare_remote_and_clone
    commit_file(clone, "new.txt", "content\n", "local only commit")
    ahead, behind = gs.get_ahead_behind(str(clone), "HEAD", "@{u}")
    assert ahead == 1
    assert behind == 0


def test_ahead_behind_when_remote_ahead(bare_remote_and_clone):
    _, origin_work, clone = bare_remote_and_clone
    commit_file(origin_work, "extra.txt", "content\n", "remote only commit")
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=origin_work, check=True)
    subprocess.run(["git", "fetch", "-q", "origin"], cwd=clone, check=True)
    ahead, behind = gs.get_ahead_behind(str(clone), "HEAD", "@{u}")
    assert ahead == 0
    assert behind == 1


def test_ahead_behind_when_diverged(bare_remote_and_clone):
    _, origin_work, clone = bare_remote_and_clone
    commit_file(origin_work, "extra.txt", "content\n", "remote only commit")
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=origin_work, check=True)
    commit_file(clone, "local_only.txt", "content\n", "local only commit")
    subprocess.run(["git", "fetch", "-q", "origin"], cwd=clone, check=True)
    ahead, behind = gs.get_ahead_behind(str(clone), "HEAD", "@{u}")
    assert ahead == 1
    assert behind == 1


# -- conflict / special-state detection ----------------------------------------


def test_detect_special_state_none_normally(repo_with_commit: Path):
    assert gs.detect_special_state(str(repo_with_commit)) is None


def test_detect_special_state_during_merge_conflict(tmp_path: Path):
    from .conftest import make_repo

    repo = make_repo(tmp_path / "conflict_repo")
    commit_file(repo, "file.txt", "line one\n", "base")
    subprocess.run(["git", "switch", "-c", "feature"], cwd=repo, check=True, capture_output=True)
    commit_file(repo, "file.txt", "line one\nfeature change\n", "feature change")
    subprocess.run(["git", "switch", "main"], cwd=repo, check=True, capture_output=True)
    commit_file(repo, "file.txt", "line one\nmain change\n", "main change")
    result = subprocess.run(
        ["git", "merge", "feature"], cwd=repo, capture_output=True, shell=False
    )
    assert result.returncode != 0  # merge should conflict

    assert gs.detect_special_state(str(repo)) == "merge"
    unmerged = gs.unmerged_files(str(repo))
    assert "file.txt" in unmerged


def test_is_ancestor(bare_remote_and_clone):
    _, _, clone = bare_remote_and_clone
    head = gs.get_head_hash(str(clone))
    upstream = gs.get_upstream(str(clone), "main")
    assert gs.is_ancestor(str(clone), head, upstream) is True

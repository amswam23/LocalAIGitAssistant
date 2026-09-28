from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from git_local_assistant import git_service as gs  # noqa: E402
from git_local_assistant import workflows as wf  # noqa: E402

from .conftest import commit_file  # noqa: E402


@pytest.fixture
def history() -> wf.HistoryLog:
    return wf.HistoryLog()


@pytest.fixture
def store() -> wf.ConfirmationStore:
    return wf.ConfirmationStore()


# -- commit gating by checks ----------------------------------------------------


def test_commit_blocked_until_checks_pass(repo_with_commit: Path, store, history):
    (repo_with_commit / "README.md").write_text("changed\n")
    with pytest.raises(wf.WorkflowError, match="Cannot commit yet"):
        wf.preview_commit(
            str(repo_with_commit), "a change", checks=None, override=False, store=store
        )


def test_commit_allowed_after_checks_pass(repo_with_commit: Path, store, history):
    (repo_with_commit / "README.md").write_text("changed\n")
    checks = wf.run_checks(str(repo_with_commit), "", "python3 -c \"pass\"", history)
    assert checks.passed is True
    pending = wf.preview_commit(
        str(repo_with_commit), "a change", checks=checks, override=False, store=store
    )
    assert pending.action == "commit"
    results = wf.execute_commit(pending, history)
    assert all(r.success for r in results)
    assert gs.is_clean(str(repo_with_commit))


def test_commit_blocked_when_checks_stale(repo_with_commit: Path, store, history):
    (repo_with_commit / "README.md").write_text("changed\n")
    checks = wf.run_checks(str(repo_with_commit), "", "python3 -c \"pass\"", history)
    # Files change again after checks ran -> checks are now stale.
    (repo_with_commit / "README.md").write_text("changed again\n")
    with pytest.raises(wf.WorkflowError, match="Cannot commit yet"):
        wf.preview_commit(
            str(repo_with_commit), "a change", checks=checks, override=False, store=store
        )


def test_commit_override_bypasses_failed_checks(repo_with_commit: Path, store, history):
    (repo_with_commit / "README.md").write_text("changed\n")
    checks = wf.run_checks(str(repo_with_commit), "", "python3 -c \"import sys; sys.exit(1)\"", history)
    assert checks.passed is False
    pending = wf.preview_commit(
        str(repo_with_commit), "a change", checks=checks, override=True, store=store
    )
    assert pending.warning is not None
    wf.execute_commit(pending, history)
    assert gs.is_clean(str(repo_with_commit))


def test_commit_rejects_empty_working_directory(repo_with_commit: Path, store, history):
    with pytest.raises(wf.WorkflowError, match="nothing to commit"):
        wf.preview_commit(str(repo_with_commit), "msg", checks=None, override=True, store=store)


def test_commit_rejects_invalid_message(repo_with_commit: Path, store, history):
    (repo_with_commit / "README.md").write_text("changed\n")
    with pytest.raises(wf.WorkflowError, match="empty"):
        wf.preview_commit(str(repo_with_commit), "   ", checks=None, override=True, store=store)


# -- confirmation / fingerprint safety -------------------------------------------


def test_repo_change_after_preview_cancels_confirmation(repo_with_commit: Path, store, history):
    (repo_with_commit / "README.md").write_text("changed\n")
    checks = wf.run_checks(str(repo_with_commit), "", "python3 -c \"pass\"", history)
    pending = wf.preview_commit(
        str(repo_with_commit), "a change", checks=checks, override=False, store=store
    )
    # Repository changes after the preview was generated.
    (repo_with_commit / "another.txt").write_text("surprise\n")
    with pytest.raises(wf.ConfirmationError, match="changed since"):
        store.consume(pending.token, "commit")


def test_confirmation_token_is_single_use(repo_with_commit: Path, store, history):
    (repo_with_commit / "README.md").write_text("changed\n")
    checks = wf.run_checks(str(repo_with_commit), "", "python3 -c \"pass\"", history)
    pending = wf.preview_commit(
        str(repo_with_commit), "a change", checks=checks, override=False, store=store
    )
    store.consume(pending.token, "commit")
    with pytest.raises(wf.ConfirmationError, match="already been used"):
        store.consume(pending.token, "commit")


def test_confirmation_expires(repo_with_commit: Path, store, history, monkeypatch):
    (repo_with_commit / "README.md").write_text("changed\n")
    checks = wf.run_checks(str(repo_with_commit), "", "python3 -c \"pass\"", history)
    pending = wf.preview_commit(
        str(repo_with_commit), "a change", checks=checks, override=False, store=store
    )
    # Simulate time passing beyond the TTL.
    real_pending = store.peek(pending.token)
    real_pending.expires_monotonic = time.monotonic() - 1
    with pytest.raises(wf.ConfirmationError, match="expired"):
        store.consume(pending.token, "commit")


def test_confirmation_action_mismatch_rejected(repo_with_commit: Path, store, history):
    (repo_with_commit / "README.md").write_text("changed\n")
    checks = wf.run_checks(str(repo_with_commit), "", "python3 -c \"pass\"", history)
    pending = wf.preview_commit(
        str(repo_with_commit), "a change", checks=checks, override=False, store=store
    )
    with pytest.raises(wf.ConfirmationError):
        store.consume(pending.token, "push")


# -- push gating --------------------------------------------------------------


def test_push_blocked_until_checks_pass(bare_remote_and_clone, store, history):
    _, _, clone = bare_remote_and_clone
    commit_file(clone, "new.txt", "content\n", "a commit")
    with pytest.raises(wf.WorkflowError, match="Cannot push yet"):
        wf.preview_push(str(clone), checks=None, override=False, store=store)


def test_push_allowed_after_checks_pass(bare_remote_and_clone, store, history):
    _, _, clone = bare_remote_and_clone
    commit_file(clone, "new.txt", "content\n", "a commit")
    checks = wf.run_checks(str(clone), "", "python3 -c \"pass\"", history)
    pending = wf.preview_push(str(clone), checks=checks, override=False, store=store)
    assert pending.action == "push"
    outcome = wf.execute_push(pending, history)
    assert outcome["push"].success is True


def test_push_stopped_when_remote_has_new_commits(bare_remote_and_clone, store, history):
    _, origin_work, clone = bare_remote_and_clone
    commit_file(clone, "local.txt", "content\n", "local commit")
    commit_file(origin_work, "remote.txt", "content\n", "remote commit")
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=origin_work, check=True)
    checks = wf.run_checks(str(clone), "", "python3 -c \"pass\"", history)
    pending = wf.preview_push(str(clone), checks=checks, override=False, store=store)
    with pytest.raises(wf.WorkflowError, match="new commit"):
        wf.execute_push(pending, history)


# -- branch creation validation --------------------------------------------------


def test_branch_creation_requires_clean_tree(repo_with_commit: Path, store):
    (repo_with_commit / "README.md").write_text("dirty\n")
    with pytest.raises(wf.WorkflowError, match="working directory has changes"):
        wf.preview_create_branch(str(repo_with_commit), "feature/x", store)


def test_branch_creation_validates_name(repo_with_commit: Path, store):
    with pytest.raises(wf.WorkflowError):
        wf.preview_create_branch(str(repo_with_commit), "bad name", store)


def test_branch_creation_succeeds_on_clean_repo(repo_with_commit: Path, store, history):
    pending = wf.preview_create_branch(str(repo_with_commit), "feature/nice", store)
    result = wf.execute_create_branch(pending, history)
    assert result.success is True
    branch, detached = gs.get_current_branch(str(repo_with_commit))
    assert branch == "feature/nice"


# -- rollback selection ----------------------------------------------------------


def test_unpushed_commit_rollback_preserves_staged_changes(repo_with_commit: Path, store, history):
    commit_file(repo_with_commit, "extra.txt", "content\n", "unpushed change")
    plan = wf.plan_undo_last_commit(str(repo_with_commit))
    assert plan.kind == "soft_reset"
    _, pending = wf.preview_undo_last_commit(str(repo_with_commit), store)
    wf.execute_undo_last_commit(pending, history)
    # The commit is gone...
    assert gs.get_head_subject(str(repo_with_commit)) != "unpushed change"
    # ...but the file changes remain staged, not discarded.
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo_with_commit,
        capture_output=True,
        text=True,
        shell=False,
    )
    assert "extra.txt" in staged.stdout


def test_pushed_commit_rollback_selects_revert(bare_remote_and_clone, store, history):
    _, _, clone = bare_remote_and_clone
    commit_file(clone, "pushed.txt", "content\n", "pushed change")
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=clone, check=True)
    plan = wf.plan_undo_last_commit(str(clone))
    assert plan.kind == "revert"
    _, pending = wf.preview_undo_last_commit(str(clone), store)
    result = wf.execute_undo_last_commit(pending, history)
    assert result.success is True


# -- conflict-state detection through workflows ----------------------------------


def test_conflict_report_reflects_merge_state(tmp_path: Path):
    from .conftest import make_repo

    repo = make_repo(tmp_path / "conflict_repo2")
    commit_file(repo, "file.txt", "line one\n", "base")
    subprocess.run(["git", "switch", "-c", "feature"], cwd=repo, check=True, capture_output=True)
    commit_file(repo, "file.txt", "line one\nfeature\n", "feature change")
    subprocess.run(["git", "switch", "main"], cwd=repo, check=True, capture_output=True)
    commit_file(repo, "file.txt", "line one\nmain\n", "main change")
    subprocess.run(["git", "merge", "feature"], cwd=repo, capture_output=True, shell=False)

    report = wf.get_conflict_report(str(repo))
    assert report.state == "merge"
    assert "file.txt" in report.files
    assert report.abort_command == "git merge --abort"


def test_no_conflict_report_when_clean(repo_with_commit: Path):
    report = wf.get_conflict_report(str(repo_with_commit))
    assert report.state is None
    assert report.files == []


# -- history log -----------------------------------------------------------------


def test_history_records_command_results(repo_with_commit: Path, history):
    result = gs.run_git(["status"], cwd=str(repo_with_commit))
    history.add_result(result)
    entries = history.recent(5)
    assert len(entries) == 1
    assert entries[0].success is True
    assert "status" in entries[0].command

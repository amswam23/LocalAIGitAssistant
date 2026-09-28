"""Workflow orchestration, safety policy, and two-step confirmation.

This module never talks to the operating system directly -- it only
calls into :mod:`git_local_assistant.git_service`. Its job is to decide
*whether* and *how* an operation should happen:

* which Git commands a requested action will run,
* whether the repository is in a state where that's currently allowed,
* issuing and checking single-use confirmation tokens bound to a
  snapshot ("fingerprint") of the repository, and
* recording everything in an in-memory command history.

Risk classification (see the project README / spec):

* READ_ONLY   -- runs immediately, never needs confirmation.
* STATE_CHANGING -- always requires a preview + explicit confirmation.
* DANGEROUS   -- not offered as a normal action at all in this MVP.
"""

from __future__ import annotations

import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from . import git_service as gs

CONFIRMATION_TTL_SECONDS = 10 * 60  # ~10 minutes


class Risk(str, Enum):
    READ_ONLY = "read_only"
    STATE_CHANGING = "state_changing"
    DANGEROUS = "dangerous"


# Reference list only (used in the UI to explain policy); not used to
# gate execution directly, since each workflow function already encodes
# the correct behaviour for the command(s) it issues.
READ_ONLY_COMMANDS = {
    "status", "diff", "log", "rev-parse", "rev-list", "remote", "symbolic-ref",
    "for-each-ref", "merge-base",
}
STATE_CHANGING_COMMANDS = {
    "fetch", "switch", "pull", "add", "commit", "push", "revert", "reset --soft",
}
DANGEROUS_COMMANDS = {
    "reset --hard", "push --force", "push --force-with-lease",
    "branch -D", "push --delete", "clean -f",
}


class WorkflowError(Exception):
    """A precondition was not met; message is safe to show to the user."""


class ConfirmationError(WorkflowError):
    """A confirmation token was invalid, expired, used, or stale."""


def _now() -> float:
    return time.monotonic()


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Pending (previewed, not-yet-executed) actions
# ---------------------------------------------------------------------------


@dataclass
class PendingAction:
    token: str
    action: str
    params: dict
    commands: list[str]  # display strings, e.g. "git switch -c my-branch"
    fingerprint: str
    repo_root: str
    created_monotonic: float
    expires_monotonic: float
    used: bool = False
    warning: str | None = None


class ConfirmationStore:
    """Holds pending actions awaiting a second, explicit confirmation."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, PendingAction] = {}

    def create(
        self,
        action: str,
        params: dict,
        commands: list[str],
        repo_root: str,
        warning: str | None = None,
    ) -> PendingAction:
        token = secrets.token_urlsafe(32)
        now = _now()
        pending = PendingAction(
            token=token,
            action=action,
            params=params,
            commands=commands,
            fingerprint=gs.fingerprint(repo_root),
            repo_root=repo_root,
            created_monotonic=now,
            expires_monotonic=now + CONFIRMATION_TTL_SECONDS,
            warning=warning,
        )
        with self._lock:
            self._purge_expired_locked()
            self._pending[token] = pending
        return pending

    def _purge_expired_locked(self) -> None:
        now = _now()
        expired = [t for t, p in self._pending.items() if p.expires_monotonic < now or p.used]
        for t in expired:
            del self._pending[t]

    def peek(self, token: str) -> PendingAction | None:
        with self._lock:
            return self._pending.get(token)

    def consume(self, token: str, expected_action: str) -> PendingAction:
        """Validate and mark a token used. Raises ConfirmationError on any problem."""
        with self._lock:
            pending = self._pending.get(token)
            if pending is None:
                raise ConfirmationError(
                    "This confirmation link is unknown or has already been used. "
                    "Please start the action again."
                )
            if pending.used:
                raise ConfirmationError(
                    "This confirmation has already been used once. "
                    "Confirmation tokens are single-use; please start again."
                )
            if pending.action != expected_action:
                raise ConfirmationError("This confirmation does not match the requested action.")
            if _now() > pending.expires_monotonic:
                del self._pending[token]
                raise ConfirmationError(
                    "This confirmation has expired (previews are valid for about 10 minutes). "
                    "Please start the action again."
                )
            current_fp = gs.fingerprint(pending.repo_root)
            if current_fp != pending.fingerprint:
                del self._pending[token]
                raise ConfirmationError(
                    "The repository changed since this action was previewed "
                    "(files, staging, or the current commit are different now). "
                    "For safety this action was cancelled -- please preview it again."
                )
            pending.used = True
            return pending


# ---------------------------------------------------------------------------
# Command history
# ---------------------------------------------------------------------------


@dataclass
class HistoryEntry:
    id: str
    timestamp: str
    command: str
    success: bool
    result: str
    exit_code: int | None = None


class HistoryLog:
    def __init__(self, max_entries: int = 200) -> None:
        self._lock = threading.Lock()
        self._entries: list[HistoryEntry] = []
        self._max = max_entries

    def add(self, command: str, success: bool, result: str, exit_code: int | None = None) -> None:
        entry = HistoryEntry(
            id=uuid.uuid4().hex,
            timestamp=_now_iso(),
            command=command,
            success=success,
            result=result[:2000],
            exit_code=exit_code,
        )
        with self._lock:
            self._entries.append(entry)
            if len(self._entries) > self._max:
                self._entries = self._entries[-self._max :]

    def add_result(self, result: gs.CommandResult) -> None:
        summary = (result.stdout.strip() or result.stderr.strip() or "(no output)")
        summary = summary.splitlines()[0] if summary else "(no output)"
        self.add(result.display_command, result.success, summary, result.returncode)

    def recent(self, n: int = 25) -> list[HistoryEntry]:
        with self._lock:
            return list(reversed(self._entries[-n:]))


# ---------------------------------------------------------------------------
# Project checks (application validation + tests)
# ---------------------------------------------------------------------------


@dataclass
class ChecksRecord:
    fingerprint: str
    app_result: gs.CommandResult | None
    test_result: gs.CommandResult | None
    timestamp: str

    @property
    def ran_anything(self) -> bool:
        return self.app_result is not None or self.test_result is not None

    @property
    def passed(self) -> bool:
        if not self.ran_anything:
            return False
        if self.app_result is not None and not self.app_result.success:
            return False
        if self.test_result is not None and not self.test_result.success:
            return False
        return True

    @property
    def failed_which(self) -> str | None:
        if self.app_result is not None and not self.app_result.success:
            return "application validation command"
        if self.test_result is not None and not self.test_result.success:
            return "test command"
        return None


def run_checks(
    repo_root: str,
    app_command: str,
    test_command: str,
    history: HistoryLog,
) -> ChecksRecord:
    """Run the configured checks from the repository root, stopping at the
    first failure, and record the exact commands and results."""
    app_result: gs.CommandResult | None = None
    test_result: gs.CommandResult | None = None
    if app_command and app_command.strip():
        app_result = gs.run_local_command(app_command, cwd=repo_root)
        history.add_result(app_result)
        if not app_result.success:
            return ChecksRecord(
                fingerprint=gs.fingerprint(repo_root),
                app_result=app_result,
                test_result=None,
                timestamp=_now_iso(),
            )
    if test_command and test_command.strip():
        test_result = gs.run_local_command(test_command, cwd=repo_root)
        history.add_result(test_result)
    return ChecksRecord(
        fingerprint=gs.fingerprint(repo_root),
        app_result=app_result,
        test_result=test_result,
        timestamp=_now_iso(),
    )


def checks_are_current(checks: ChecksRecord | None, repo_root: str) -> bool:
    if checks is None:
        return False
    return checks.fingerprint == gs.fingerprint(repo_root)


def can_commit(checks: ChecksRecord | None, repo_root: str, override: bool) -> tuple[bool, str]:
    """Return (allowed, reason)."""
    if override:
        return True, "Proceeding with an explicit override of project checks."
    if checks is None or not checks.ran_anything:
        return False, "No project checks have been run yet for the current files."
    if not checks_are_current(checks, repo_root):
        return False, "Files or the current commit changed since checks last ran; run checks again."
    if not checks.passed:
        return False, f"The {checks.failed_which} failed."
    return True, "Checks passed for the current files."


def can_push(checks: ChecksRecord | None, repo_root: str, override: bool) -> tuple[bool, str]:
    return can_commit(checks, repo_root, override)


# ---------------------------------------------------------------------------
# Start new work: check remote (fetch) + create branch
# ---------------------------------------------------------------------------


def preview_fetch(repo_root: str, remote: str, store: ConfirmationStore) -> PendingAction:
    commands = [f"git fetch {remote}"]
    return store.create("fetch", {"remote": remote}, commands, repo_root)


def execute_fetch(pending: PendingAction, history: HistoryLog) -> gs.CommandResult:
    remote = pending.params["remote"]
    result = gs.run_git(["fetch", remote], cwd=pending.repo_root)
    history.add_result(result)
    if not result.success:
        raise WorkflowError(
            f"'git fetch {remote}' failed:\n{result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def preview_create_branch(
    repo_root: str, branch_name: str, store: ConfirmationStore
) -> PendingAction:
    error = gs.validate_branch_name(branch_name)
    if error:
        raise WorkflowError(error)
    if not gs.is_clean(repo_root):
        raise WorkflowError(
            "Your working directory has changes. Commit, stash, or discard them "
            "elsewhere before creating a new branch."
        )
    branch, detached = gs.get_current_branch(repo_root)
    if not detached and branch:
        upstream_ref = gs.get_upstream(repo_root, branch)
        if upstream_ref:
            try:
                ahead, behind = gs.get_ahead_behind(repo_root, "HEAD", "@{u}")
                if behind > 0:
                    raise WorkflowError(
                        f"Your base branch '{branch}' is {behind} commit(s) behind "
                        f"{upstream_ref}. Fetch and pull first so your new branch "
                        "starts from up-to-date code."
                    )
            except gs.GitError:
                pass
    branch_name = branch_name.strip()
    commands = [f"git switch -c {branch_name}"]
    return store.create("create_branch", {"branch_name": branch_name}, commands, repo_root)


def execute_create_branch(pending: PendingAction, history: HistoryLog) -> gs.CommandResult:
    name = pending.params["branch_name"]
    result = gs.run_git(["switch", "-c", name], cwd=pending.repo_root)
    history.add_result(result)
    if not result.success:
        raise WorkflowError(
            f"'git switch -c {name}' failed:\n{result.stderr.strip() or result.stdout.strip()}"
        )
    return result


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------

_INVALID_MSG_CHARS = ("\r", "\n")


def validate_commit_message(message: str) -> str | None:
    if not message or not message.strip():
        return "Commit message cannot be empty."
    if len(message) > 500:
        return "Commit message is too long (please keep it under 500 characters)."
    if any(ch in message for ch in _INVALID_MSG_CHARS):
        return "Commit message must be a single line (no line breaks)."
    return None


def preview_commit(
    repo_root: str,
    message: str,
    checks: ChecksRecord | None,
    override: bool,
    store: ConfirmationStore,
) -> PendingAction:
    error = validate_commit_message(message)
    if error:
        raise WorkflowError(error)
    if gs.is_clean(repo_root):
        raise WorkflowError("There is nothing to commit -- the working directory is clean.")
    allowed, reason = can_commit(checks, repo_root, override)
    if not allowed:
        raise WorkflowError(
            f"Cannot commit yet: {reason} "
            "Run checks first, or explicitly check the override box if you understand the risk."
        )
    message = message.strip()
    commands = ["git add --all", f"git commit -m {message!r}"]
    warning = "Checks were overridden for this commit." if override and checks and not checks.passed else None
    if override and (checks is None or not checks.ran_anything):
        warning = "No checks were run at all for this commit (override used)."
    return store.create(
        "commit", {"message": message, "override": override}, commands, repo_root, warning=warning
    )


def execute_commit(pending: PendingAction, history: HistoryLog) -> list[gs.CommandResult]:
    add_result = gs.run_git(["add", "--all"], cwd=pending.repo_root)
    history.add_result(add_result)
    if not add_result.success:
        raise WorkflowError("'git add --all' failed:\n" + (add_result.stderr.strip() or ""))
    message = pending.params["message"]
    commit_result = gs.run_git(["commit", "-m", message], cwd=pending.repo_root)
    history.add_result(commit_result)
    if not commit_result.success:
        raise WorkflowError(
            "'git commit' failed:\n" + (commit_result.stderr.strip() or commit_result.stdout.strip())
        )
    return [add_result, commit_result]


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------


def preview_push(
    repo_root: str,
    checks: ChecksRecord | None,
    override: bool,
    store: ConfirmationStore,
) -> PendingAction:
    branch, detached = gs.get_current_branch(repo_root)
    if detached:
        raise WorkflowError("You are in a detached HEAD state. Switch to a branch before pushing.")
    if not gs.is_clean(repo_root):
        raise WorkflowError("Your working directory has uncommitted changes. Commit them first.")
    allowed, reason = can_push(checks, repo_root, override)
    if not allowed:
        raise WorkflowError(
            f"Cannot push yet: {reason} "
            "Run checks first, or explicitly check the override box if you understand the risk."
        )
    remote = gs.choose_default_remote(repo_root)
    if not remote:
        raise WorkflowError("This repository has no remote configured; nothing to push to.")
    upstream_ref = gs.get_upstream(repo_root, branch)
    if upstream_ref:
        commands = [f"git fetch {remote}", f"git push {remote} {branch}"]
        set_upstream = False
    else:
        commands = [f"git fetch {remote}", f"git push --set-upstream {remote} {branch}"]
        set_upstream = True
    return store.create(
        "push",
        {"remote": remote, "branch": branch, "set_upstream": set_upstream, "override": override},
        commands,
        repo_root,
    )


def execute_push(pending: PendingAction, history: HistoryLog) -> dict:
    remote = pending.params["remote"]
    branch = pending.params["branch"]
    set_upstream = pending.params["set_upstream"]

    fetch_result = gs.run_git(["fetch", remote], cwd=pending.repo_root)
    history.add_result(fetch_result)
    if not fetch_result.success:
        raise WorkflowError(
            f"'git fetch {remote}' failed, so the push was not attempted:\n"
            + (fetch_result.stderr.strip() or "")
        )

    if not set_upstream:
        try:
            _, behind = gs.get_ahead_behind(repo_root=pending.repo_root, local_ref="HEAD", upstream_ref="@{u}")
        except gs.GitError:
            behind = 0
        if behind > 0:
            raise WorkflowError(
                f"The remote branch has {behind} new commit(s) that you don't have. "
                "Pushing was stopped to avoid rejecting your push or losing history. "
                "Pull (merge or rebase) first, then try pushing again."
            )

    commits_before = _list_unpushed_commits(pending.repo_root, remote, branch, set_upstream)

    if set_upstream:
        push_args = ["push", "--set-upstream", remote, branch]
    else:
        push_args = ["push", remote, branch]
    push_result = gs.run_git(push_args, cwd=pending.repo_root)
    history.add_result(push_result)
    if not push_result.success:
        raise WorkflowError(
            "'" + push_result.display_command + "' failed:\n"
            + (push_result.stderr.strip() or push_result.stdout.strip())
        )
    return {"fetch": fetch_result, "push": push_result, "pushed_commits": commits_before}


def _list_unpushed_commits(repo_root: str, remote: str, branch: str, set_upstream: bool) -> list[str]:
    """Best-effort list of commit hashes about to be pushed (newest first)."""
    try:
        remote_ref = f"{remote}/{branch}"
        range_spec = f"{remote_ref}..HEAD" if not set_upstream else "HEAD"
        result = gs.run_git(["rev-list", range_spec], cwd=repo_root)
        if result.success:
            return [line.strip() for line in result.stdout.splitlines() if line.strip()]
    except Exception:
        pass
    return []


# ---------------------------------------------------------------------------
# Pull
# ---------------------------------------------------------------------------


def preview_pull(repo_root: str, mode: str, store: ConfirmationStore) -> PendingAction:
    if mode not in ("merge", "rebase"):
        raise WorkflowError("Pull mode must be 'merge' or 'rebase'.")
    if not gs.is_clean(repo_root):
        raise WorkflowError("Your working directory has uncommitted changes. Commit them first.")
    branch, detached = gs.get_current_branch(repo_root)
    if detached:
        raise WorkflowError("You are in a detached HEAD state. Switch to a branch before pulling.")
    upstream_ref = gs.get_upstream(repo_root, branch)
    if not upstream_ref:
        raise WorkflowError("This branch has no upstream branch configured; there is nothing to pull.")
    remote, _, remote_branch = upstream_ref.partition("/")
    flag = "--no-rebase" if mode == "merge" else "--rebase"
    commands = [f"git pull {flag} {remote} {remote_branch}"]
    return store.create(
        "pull", {"remote": remote, "remote_branch": remote_branch, "mode": mode}, commands, repo_root
    )


def execute_pull(pending: PendingAction, history: HistoryLog) -> gs.CommandResult:
    remote = pending.params["remote"]
    remote_branch = pending.params["remote_branch"]
    mode = pending.params["mode"]
    flag = "--no-rebase" if mode == "merge" else "--rebase"
    result = gs.run_git(["pull", flag, remote, remote_branch], cwd=pending.repo_root)
    history.add_result(result)
    # Note: a non-zero return code here can legitimately mean "conflicts" --
    # the caller inspects the repository's special state afterwards rather
    # than treating this purely as failure.
    return result


# ---------------------------------------------------------------------------
# Conflict handling (read-only reporting + abort)
# ---------------------------------------------------------------------------

CONFLICT_MARKERS = ("<<<<<<<", "=======", ">>>>>>>")

_ABORT_COMMAND = {
    "merge": ["merge", "--abort"],
    "rebase": ["rebase", "--abort"],
    "revert": ["revert", "--abort"],
    "cherry-pick": ["cherry-pick", "--abort"],
}

_CONTINUE_HINT = {
    "merge": "git commit",
    "rebase": "git rebase --continue",
    "revert": "git revert --continue",
    "cherry-pick": "git cherry-pick --continue",
}


@dataclass
class ConflictReport:
    state: str | None
    files: list[str]
    continue_hint: str | None
    abort_command: str | None


def get_conflict_report(repo_root: str) -> ConflictReport:
    state = gs.detect_special_state(repo_root)
    files = gs.unmerged_files(repo_root)
    if state is None and not files:
        return ConflictReport(state=None, files=[], continue_hint=None, abort_command=None)
    continue_hint = _CONTINUE_HINT.get(state) if state else None
    abort_command = ("git " + " ".join(_ABORT_COMMAND[state])) if state else None
    return ConflictReport(state=state, files=files, continue_hint=continue_hint, abort_command=abort_command)


def preview_abort(repo_root: str, state: str, store: ConfirmationStore) -> PendingAction:
    if state not in _ABORT_COMMAND:
        raise WorkflowError(f"Unknown conflict state '{state}'.")
    commands = ["git " + " ".join(_ABORT_COMMAND[state])]
    return store.create("abort", {"state": state}, commands, repo_root)


def execute_abort(pending: PendingAction, history: HistoryLog) -> gs.CommandResult:
    state = pending.params["state"]
    result = gs.run_git(_ABORT_COMMAND[state], cwd=pending.repo_root)
    history.add_result(result)
    if not result.success:
        raise WorkflowError(
            f"Abort command failed:\n{result.stderr.strip() or result.stdout.strip()}"
        )
    return result


# ---------------------------------------------------------------------------
# Rollback / revert
# ---------------------------------------------------------------------------


@dataclass
class UndoPlan:
    kind: str  # "soft_reset" or "revert"
    commit_hash: str
    commit_short: str
    commit_subject: str
    explanation: str


def plan_undo_last_commit(repo_root: str) -> UndoPlan:
    if not gs._has_commits(repo_root):  # noqa: SLF001 - internal helper reuse within package
        raise WorkflowError("There are no commits yet to undo.")
    head_hash = gs.get_head_hash(repo_root)
    head_short = gs.get_head_short_hash(repo_root)
    head_subject = gs.get_head_subject(repo_root)
    branch, detached = gs.get_current_branch(repo_root)
    upstream_ref = gs.get_upstream(repo_root, branch) if not detached else None
    if upstream_ref and gs.is_ancestor(repo_root, head_hash, upstream_ref):
        return UndoPlan(
            kind="revert",
            commit_hash=head_hash,
            commit_short=head_short,
            commit_subject=head_subject,
            explanation=(
                f"Commit {head_short} already exists on {upstream_ref}, so rewriting history "
                "would be unsafe. Git will create a new commit that reverses it."
            ),
        )
    return UndoPlan(
        kind="soft_reset",
        commit_hash=head_hash,
        commit_short=head_short,
        commit_subject=head_subject,
        explanation=(
            f"Commit {head_short} has not been pushed, so it can be safely removed. "
            "Its file changes will remain staged, not discarded."
        ),
    )


def preview_undo_last_commit(repo_root: str, store: ConfirmationStore) -> tuple[UndoPlan, PendingAction]:
    plan = plan_undo_last_commit(repo_root)
    if plan.kind == "soft_reset":
        commands = [f"git reset --soft {plan.commit_hash}^"]
    else:
        commands = [f"git revert --no-edit {plan.commit_hash}"]
    pending = store.create(
        "undo_last_commit",
        {"kind": plan.kind, "commit_hash": plan.commit_hash},
        commands,
        repo_root,
    )
    return plan, pending


def execute_undo_last_commit(pending: PendingAction, history: HistoryLog) -> gs.CommandResult:
    kind = pending.params["kind"]
    commit_hash = pending.params["commit_hash"]
    if kind == "soft_reset":
        result = gs.run_git(["reset", "--soft", f"{commit_hash}^"], cwd=pending.repo_root)
    else:
        result = gs.run_git(["revert", "--no-edit", commit_hash], cwd=pending.repo_root)
    history.add_result(result)
    if not result.success:
        raise WorkflowError(
            f"'{result.display_command}' failed:\n{result.stderr.strip() or result.stdout.strip()}"
        )
    return result

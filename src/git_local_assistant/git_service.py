"""Low-level Git access.

This module is the *only* place in the application that runs external
processes. Every function here either:

  * runs Git with an argument list and ``shell=False`` (never a shell
    string built from user input), or
  * inspects the ``.git`` directory directly (read-only, no subprocess).

Nothing in this module decides *whether* an operation is safe to run --
that policy lives in :mod:`git_local_assistant.workflows`. This module
only knows how to ask Git questions and, when told to, run a Git command
and faithfully report what happened.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_TIMEOUT = 15
LONG_TIMEOUT = 600  # for user-configured project/test commands


class GitError(Exception):
    """Raised when a Git operation cannot be completed.

    The message is always written to be understandable by a non-expert:
    it explains *what* was being attempted and *why* it did not work.
    """


class NotAGitRepositoryError(GitError):
    """Raised when a selected folder is not inside a Git repository."""


class GitNotInstalledError(GitError):
    """Raised when the ``git`` executable cannot be found on PATH."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class CommandResult:
    """The full, honest result of running one external command."""

    command: list[str]
    cwd: str
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    timestamp: str
    timed_out: bool = False

    @property
    def success(self) -> bool:
        return (not self.timed_out) and self.returncode == 0

    @property
    def display_command(self) -> str:
        return " ".join(shlex.quote(part) for part in self.command)


def git_available() -> bool:
    """Return True if a ``git`` executable is on PATH."""
    return shutil.which("git") is not None


def run_git(
    args: list[str],
    cwd: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> CommandResult:
    """Run ``git <args>`` in ``cwd`` with ``shell=False``.

    ``args`` must already be a list of separate arguments -- this
    function never builds or interprets a shell command string.
    """
    if not git_available():
        raise GitNotInstalledError(
            "Git does not appear to be installed, or is not on your PATH. "
            "Install Git from https://git-scm.com/downloads and try again."
        )
    full_command = ["git", *args]
    start = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.run(
            full_command,
            cwd=cwd,
            shell=False,
            capture_output=True,
            timeout=timeout,
        )
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        returncode = proc.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        returncode = -1
        stdout = (exc.stdout or b"").decode("utf-8", errors="replace")
        stderr = (
            (exc.stderr or b"").decode("utf-8", errors="replace")
            + f"\n[git-local-assistant] Command timed out after {timeout}s."
        )
    duration = time.monotonic() - start
    return CommandResult(
        command=full_command,
        cwd=cwd,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=duration,
        timestamp=_now_iso(),
        timed_out=timed_out,
    )


def run_local_command(command_text: str, cwd: str, timeout: int = LONG_TIMEOUT) -> CommandResult:
    """Run a user-configured local command (e.g. a test command).

    The command is supplied by the user as a single line of text (for
    example ``pytest -q`` or ``npm test``). It is parsed with
    :func:`shlex.split` -- which correctly handles quoted paths -- into
    an argument list, and is then executed directly with
    ``shell=False``. No shell is ever involved, so shell metacharacters
    (``;``, ``&&``, ``|``, backticks, etc.) have no special meaning and
    cannot be used to chain or inject additional commands.
    """
    try:
        argv = shlex.split(command_text, posix=(os.name != "nt"))
    except ValueError as exc:
        raise GitError(f"Could not parse the command '{command_text}': {exc}") from exc
    if not argv:
        raise GitError("The configured command is empty.")
    start = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            shell=False,
            capture_output=True,
            timeout=timeout,
        )
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        returncode = proc.returncode
    except FileNotFoundError as exc:
        raise GitError(
            f"Could not find the program '{argv[0]}' to run the configured command. "
            "Make sure it is installed and on your PATH."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        returncode = -1
        stdout = (exc.stdout or b"").decode("utf-8", errors="replace")
        stderr = (
            (exc.stderr or b"").decode("utf-8", errors="replace")
            + f"\n[git-local-assistant] Command timed out after {timeout}s."
        )
    duration = time.monotonic() - start
    return CommandResult(
        command=argv,
        cwd=cwd,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=duration,
        timestamp=_now_iso(),
        timed_out=timed_out,
    )


def resolve_repo_root(path_text: str) -> str:
    """Validate that ``path_text`` exists and is inside a Git repository.

    Returns the absolute repository root (the same folder ``git`` itself
    would use), so that all later commands operate on a consistent path
    regardless of which subfolder the user typed.
    """
    if not path_text or not path_text.strip():
        raise GitError("Please enter a folder path.")
    candidate = Path(path_text).expanduser()
    if not candidate.exists():
        raise GitError(f"The folder '{path_text}' does not exist.")
    if not candidate.is_dir():
        raise GitError(f"'{path_text}' is a file, not a folder.")
    result = run_git(["rev-parse", "--show-toplevel"], cwd=str(candidate))
    if not result.success:
        raise NotAGitRepositoryError(
            f"'{path_text}' does not appear to be inside a Git repository "
            "(git rev-parse --show-toplevel failed). "
            "Choose a folder that is a Git project, or run 'git init' there first."
        )
    root = result.stdout.strip()
    # On Windows, git prints a path with forward slashes; normalise it.
    return str(Path(root).resolve())


# ---------------------------------------------------------------------------
# Read-only status helpers
# ---------------------------------------------------------------------------


@dataclass
class ChangedFile:
    """One line of ``git status --porcelain`` output."""

    index_status: str  # single char, staged/index status (may be ' ')
    worktree_status: str  # single char, working-tree status (may be ' ')
    path: str
    orig_path: str | None = None  # set for renames

    @property
    def code(self) -> str:
        return f"{self.index_status}{self.worktree_status}"

    @property
    def is_untracked(self) -> bool:
        return self.code == "??"


def get_status_porcelain(repo_root: str) -> list[ChangedFile]:
    """Return the parsed output of ``git status --porcelain=v1 -z``.

    The ``-z`` (NUL-terminated) form is used so that filenames containing
    spaces or unusual characters are parsed correctly, and so that the
    two meaningful leading status-code characters (which may themselves
    be spaces) are never accidentally stripped.
    """
    result = run_git(["status", "--porcelain=v1", "-z"], cwd=repo_root)
    if not result.success:
        raise GitError(
            "Could not read the repository status (git status failed):\n" + result.stderr.strip()
        )
    entries: list[ChangedFile] = []
    raw = result.stdout
    if not raw:
        return entries
    parts = raw.split("\0")
    i = 0
    while i < len(parts):
        chunk = parts[i]
        i += 1
        if not chunk:
            continue
        # Each entry is "XY <path>"; renames have an extra NUL-separated
        # original path that follows immediately.
        code = chunk[:2]
        path = chunk[3:]
        index_status, worktree_status = code[0], code[1]
        orig_path = None
        if index_status == "R" or worktree_status == "R":
            if i < len(parts):
                orig_path = parts[i]
                i += 1
        entries.append(
            ChangedFile(
                index_status=index_status,
                worktree_status=worktree_status,
                path=path,
                orig_path=orig_path,
            )
        )
    return entries


def is_clean(repo_root: str) -> bool:
    """True if there are no changed, staged, or untracked files."""
    return len(get_status_porcelain(repo_root)) == 0


def get_head_hash(repo_root: str) -> str:
    result = run_git(["rev-parse", "HEAD"], cwd=repo_root)
    if not result.success:
        raise GitError(
            "Could not determine the current commit "
            "(the repository may not have any commits yet):\n" + result.stderr.strip()
        )
    return result.stdout.strip()


def get_head_short_hash(repo_root: str) -> str:
    result = run_git(["rev-parse", "--short", "HEAD"], cwd=repo_root)
    if not result.success:
        return "(none)"
    return result.stdout.strip()


def get_head_subject(repo_root: str) -> str:
    result = run_git(["log", "-1", "--pretty=%s"], cwd=repo_root)
    if not result.success:
        return "(no commits yet)"
    return result.stdout.strip()


def get_current_branch(repo_root: str) -> tuple[str | None, bool]:
    """Return ``(branch_name_or_None, is_detached)``."""
    result = run_git(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=repo_root)
    if result.success:
        return result.stdout.strip(), False
    return None, True


def get_upstream(repo_root: str, branch: str | None) -> str | None:
    """Return the upstream ref (e.g. ``origin/main``) or ``None``."""
    if branch is None:
        return None
    result = run_git(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        cwd=repo_root,
    )
    if not result.success:
        return None
    return result.stdout.strip()


def get_ahead_behind(repo_root: str, local_ref: str, upstream_ref: str) -> tuple[int, int]:
    """Return ``(ahead, behind)`` counts of ``local_ref`` vs ``upstream_ref``."""
    result = run_git(
        ["rev-list", "--left-right", "--count", f"{local_ref}...{upstream_ref}"],
        cwd=repo_root,
    )
    if not result.success:
        raise GitError(
            "Could not compare the local branch with its upstream:\n" + result.stderr.strip()
        )
    parts = result.stdout.strip().split()
    if len(parts) != 2:
        raise GitError("Unexpected output while comparing commits ahead/behind.")
    return int(parts[0]), int(parts[1])


def list_remotes(repo_root: str) -> list[str]:
    result = run_git(["remote"], cwd=repo_root)
    if not result.success:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def choose_default_remote(repo_root: str) -> str | None:
    remotes = list_remotes(repo_root)
    if not remotes:
        return None
    return "origin" if "origin" in remotes else remotes[0]


def fingerprint(repo_root: str) -> str:
    """A short, stable fingerprint of "everything that matters" right now.

    It combines HEAD and the complete working-tree status, so that *any*
    commit, checkout, stage, unstage, edit, or new/deleted file changes
    the fingerprint. Confirmation screens compare this value at preview
    time and at execution time, and refuse to act if it has changed.
    """
    head_result = run_git(["rev-parse", "HEAD"], cwd=repo_root)
    head = head_result.stdout.strip() if head_result.success else "(no-commits)"
    status_result = run_git(["status", "--porcelain=v1", "-z"], cwd=repo_root)
    status_raw = status_result.stdout if status_result.success else ""
    branch, detached = get_current_branch(repo_root)
    # The porcelain status line for an already-modified file does not change
    # if the file is edited *again* (it is still just "M"), so the actual
    # diff content -- both unstaged and staged -- must also be included.
    # Otherwise a second edit to an already-dirty file would not change the
    # fingerprint, and a stale confirmation could slip through unnoticed.
    unstaged_diff = run_git(["diff", "--no-color"], cwd=repo_root)
    staged_diff = run_git(["diff", "--no-color", "--cached"], cwd=repo_root)
    payload = "\x1f".join(
        [
            head,
            "detached" if detached else (branch or ""),
            status_raw,
            unstaged_diff.stdout if unstaged_diff.success else "",
            staged_diff.stdout if staged_diff.success else "",
        ]
    )
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()[:16]


@dataclass
class UpstreamInfo:
    remote: str | None
    remote_branch: str | None
    ahead: int = 0
    behind: int = 0
    has_upstream: bool = False

    @property
    def sync_status(self) -> str:
        if not self.has_upstream:
            return "no-upstream"
        if self.ahead == 0 and self.behind == 0:
            return "synchronized"
        if self.ahead > 0 and self.behind == 0:
            return "ahead"
        if self.behind > 0 and self.ahead == 0:
            return "behind"
        return "diverged"


@dataclass
class RepoStatus:
    repo_root: str
    branch: str | None
    detached: bool
    head_hash: str
    head_short_hash: str
    head_subject: str
    remote: str | None
    upstream: UpstreamInfo
    changed_files: list[ChangedFile] = field(default_factory=list)
    fingerprint_value: str = ""

    @property
    def is_clean(self) -> bool:
        return len(self.changed_files) == 0

    @property
    def untracked_files(self) -> list[ChangedFile]:
        return [f for f in self.changed_files if f.is_untracked]

    @property
    def relationship_plain_language(self) -> str:
        u = self.upstream
        if self.detached:
            return "You are not on a branch, so there is no remote relationship to describe."
        if not u.has_upstream:
            return "This branch is not connected to any remote branch yet."
        status = u.sync_status
        if status == "synchronized":
            return f"Your branch is up to date with {u.remote}/{u.remote_branch}."
        if status == "ahead":
            return (
                f"Your branch has {u.ahead} commit(s) that {u.remote}/{u.remote_branch} "
                "does not have yet. You can push."
            )
        if status == "behind":
            return (
                f"{u.remote}/{u.remote_branch} has {u.behind} commit(s) you don't have yet. "
                "You can pull."
            )
        return (
            f"Your branch and {u.remote}/{u.remote_branch} have both moved on "
            f"({u.ahead} local, {u.behind} remote). They have diverged; "
            "you'll need to pull (merge or rebase) before you can push."
        )


def build_repo_status(repo_root: str) -> RepoStatus:
    """Gather a complete, read-only picture of the repository right now."""
    branch, detached = get_current_branch(repo_root)
    head_hash = get_head_hash(repo_root) if _has_commits(repo_root) else "(no commits yet)"
    head_short = get_head_short_hash(repo_root)
    head_subject = get_head_subject(repo_root)
    remote = choose_default_remote(repo_root)
    upstream_ref = get_upstream(repo_root, branch) if not detached else None
    upstream = UpstreamInfo(remote=remote, remote_branch=None)
    if upstream_ref:
        up_remote, _, up_branch = upstream_ref.partition("/")
        ahead, behind = get_ahead_behind(repo_root, "HEAD", "@{u}")
        upstream = UpstreamInfo(
            remote=up_remote,
            remote_branch=up_branch,
            ahead=ahead,
            behind=behind,
            has_upstream=True,
        )
    changed = get_status_porcelain(repo_root)
    return RepoStatus(
        repo_root=repo_root,
        branch=branch,
        detached=detached,
        head_hash=head_hash,
        head_short_hash=head_short,
        head_subject=head_subject,
        remote=remote,
        upstream=upstream,
        changed_files=changed,
        fingerprint_value=fingerprint(repo_root),
    )


def _has_commits(repo_root: str) -> bool:
    result = run_git(["rev-parse", "--verify", "-q", "HEAD"], cwd=repo_root)
    return result.success


# ---------------------------------------------------------------------------
# Branches
# ---------------------------------------------------------------------------

_REF_FORMAT = "%(refname)\t%(objectname:short)\t%(subject)\t%(upstream:short)\t%(HEAD)"


@dataclass
class BranchInfo:
    name: str
    is_remote: bool
    is_current: bool
    short_hash: str
    subject: str
    upstream: str | None
    ahead: int = 0
    behind: int = 0


def list_branches(repo_root: str) -> list[BranchInfo]:
    """Return local and remote branches via ``git for-each-ref``.

    Uses a fixed, tab-separated format string (never raw/free-form Git
    output) so the result can be parsed safely field-by-field. Symbolic
    remote HEAD pointers (e.g. ``origin/HEAD``) are excluded.
    """
    result = run_git(
        [
            "for-each-ref",
            f"--format={_REF_FORMAT}",
            "refs/heads",
            "refs/remotes",
        ],
        cwd=repo_root,
    )
    if not result.success:
        raise GitError("Could not list branches:\n" + result.stderr.strip())
    branches: list[BranchInfo] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        while len(fields) < 5:
            fields.append("")
        refname, short_hash, subject, upstream, is_head = fields[:5]
        is_remote = refname.startswith("refs/remotes/")
        if is_remote and refname.endswith("/HEAD"):
            continue  # symbolic remote HEAD pointer, not a real branch
        if is_remote:
            name = refname[len("refs/remotes/") :]
        else:
            name = refname[len("refs/heads/") :]
        is_current = is_head.strip() == "*"
        ahead = behind = 0
        if upstream:
            try:
                ahead, behind = get_ahead_behind(repo_root, refname, f"refs/remotes/{upstream}")
            except GitError:
                pass
        branches.append(
            BranchInfo(
                name=name,
                is_remote=is_remote,
                is_current=is_current,
                short_hash=short_hash or "(none)",
                subject=subject,
                upstream=upstream or None,
                ahead=ahead,
                behind=behind,
            )
        )
    return branches


def validate_branch_name(name: str) -> str | None:
    """Return an error message if ``name`` is not a valid branch name."""
    if not name or not name.strip():
        return "Branch name cannot be empty."
    name = name.strip()
    if any(ch.isspace() for ch in name):
        return "Branch name cannot contain spaces."
    result = subprocess.run(
        ["git", "check-ref-format", "--branch", name],
        capture_output=True,
        shell=False,
    )
    if result.returncode != 0:
        return (
            f"'{name}' is not a valid Git branch name. "
            "Avoid spaces, '..', '~', '^', ':', '?', '*', '[', trailing '.lock', "
            "and leading/trailing '/'."
        )
    return None


# ---------------------------------------------------------------------------
# Commit graph
# ---------------------------------------------------------------------------

_GRAPH_FORMAT = "%h%x09%d%x09%s%x09%an%x09%ad"


def commit_graph_lines(repo_root: str, max_commits: int = 30) -> list[str]:
    """Return raw ``git log --graph`` lines (read-only), safe to display.

    The ASCII graph characters (``* | / \\``) are produced by Git itself
    and are preserved verbatim; the commit metadata after them uses a
    fixed tab-separated format so it can still be HTML-escaped safely by
    the caller before display.
    """
    result = run_git(
        [
            "log",
            "--graph",
            "--all",
            "--decorate",
            "--date=short",
            f"--pretty=format:{_GRAPH_FORMAT}",
            f"-n{max_commits}",
        ],
        cwd=repo_root,
    )
    if not result.success:
        if "does not have any commits yet" in result.stderr or not result.stdout.strip():
            return []
        raise GitError("Could not build the commit graph:\n" + result.stderr.strip())
    return result.stdout.splitlines()


# ---------------------------------------------------------------------------
# Conflict / special-state detection (filesystem only, read-only)
# ---------------------------------------------------------------------------


def git_dir(repo_root: str) -> Path:
    result = run_git(["rev-parse", "--git-dir"], cwd=repo_root)
    if not result.success:
        raise GitError("Could not locate the .git directory.")
    p = Path(result.stdout.strip())
    if not p.is_absolute():
        p = Path(repo_root) / p
    return p


def detect_special_state(repo_root: str) -> str | None:
    """Return one of 'merge', 'rebase', 'revert', 'cherry-pick', or None."""
    gd = git_dir(repo_root)
    if (gd / "MERGE_HEAD").exists():
        return "merge"
    if (gd / "rebase-merge").exists() or (gd / "rebase-apply").exists():
        return "rebase"
    if (gd / "REVERT_HEAD").exists():
        return "revert"
    if (gd / "CHERRY_PICK_HEAD").exists():
        return "cherry-pick"
    return None


def unmerged_files(repo_root: str) -> list[str]:
    result = run_git(["diff", "--name-only", "--diff-filter=U"], cwd=repo_root)
    if not result.success:
        raise GitError("Could not list conflicted files:\n" + result.stderr.strip())
    return [line for line in result.stdout.splitlines() if line.strip()]


def is_ancestor(repo_root: str, ancestor_ref: str, ref: str) -> bool:
    """True if ``ancestor_ref`` is an ancestor of (already contained in) ``ref``."""
    result = run_git(["merge-base", "--is-ancestor", ancestor_ref, ref], cwd=repo_root)
    return result.success


def diff_stat(repo_root: str, staged: bool = False) -> str:
    args = ["diff", "--stat"]
    if staged:
        args.insert(1, "--cached")
    result = run_git(args, cwd=repo_root)
    if not result.success:
        raise GitError("Could not compute diff statistics:\n" + result.stderr.strip())
    return result.stdout

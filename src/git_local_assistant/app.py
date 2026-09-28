"""Browser dashboard: HTTP server, routing, and HTML rendering.

This module is deliberately the *only* place that knows about HTTP or
HTML. It calls into :mod:`git_local_assistant.git_service` (read-only
Git queries) and :mod:`git_local_assistant.workflows` (safety policy and
state-changing actions) but contains no Git subprocess calls itself.
"""

from __future__ import annotations

import secrets
import socket
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from html import escape as esc
from http import server
from urllib.parse import parse_qs, urlsplit

from . import git_service as gs
from . import workflows as wf

APP_TITLE = "Git Local Assistant"


# ---------------------------------------------------------------------------
# Application state (single local user, single process)
# ---------------------------------------------------------------------------


@dataclass
class AppState:
    repo_root: str | None = None
    csrf_token: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    app_command: str = ""
    test_command: str = ""
    checks: wf.ChecksRecord | None = None
    confirmations: wf.ConfirmationStore = field(default_factory=wf.ConfirmationStore)
    history: wf.HistoryLog = field(default_factory=wf.HistoryLog)
    flash: str | None = None
    flash_kind: str = "info"  # info | success | error
    lock: threading.Lock = field(default_factory=threading.Lock)

    def set_flash(self, message: str, kind: str = "info") -> None:
        self.flash = message
        self.flash_kind = kind

    def pop_flash(self) -> tuple[str | None, str]:
        message, kind = self.flash, self.flash_kind
        self.flash = None
        self.flash_kind = "info"
        return message, kind


# ---------------------------------------------------------------------------
# HTML page shell + small components
# ---------------------------------------------------------------------------

CSS = """
:root {
  --bg: #f5f7fa; --card: #ffffff; --text: #1c2530; --muted: #5b6b7c;
  --border: #dbe2ea; --accent: #2f6fed; --accent-dark: #1d4fb8;
  --green: #1f8a4c; --green-bg: #e6f6ec;
  --blue: #2f6fed; --blue-bg: #e8f0fe;
  --orange: #b5680a; --orange-bg: #fdf1e0;
  --red: #c22b2b; --red-bg: #fbe8e8;
  --gray: #6b7280; --gray-bg: #eef0f2;
  --mono: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
  line-height: 1.45;
}
header.top {
  background: var(--text); color: #fff; padding: 14px 24px;
  display: flex; align-items: center; justify-content: space-between;
}
header.top a { color: #fff; text-decoration: none; font-weight: 600; }
main { max-width: 1080px; margin: 0 auto; padding: 20px 20px 60px; }
h1 { font-size: 1.4rem; margin: 0; }
h2 { font-size: 1.1rem; margin: 0 0 12px; }
h3 { font-size: 0.95rem; margin: 0 0 8px; }
.card {
  background: var(--card); border: 1px solid var(--border); border-radius: 10px;
  padding: 18px 20px; margin-bottom: 18px;
}
.muted { color: var(--muted); font-size: 0.9rem; }
.flash { border-radius: 8px; padding: 12px 16px; margin-bottom: 18px; font-size: 0.95rem; }
.flash.success { background: var(--green-bg); color: var(--green); border: 1px solid var(--green); }
.flash.error { background: var(--red-bg); color: var(--red); border: 1px solid var(--red); }
.flash.info { background: var(--blue-bg); color: var(--accent-dark); border: 1px solid var(--accent); }
code, pre, .mono { font-family: var(--mono); }
pre.cmd, code.cmd {
  display: inline-block; background: #1c2530; color: #eaf1ff; padding: 2px 8px;
  border-radius: 6px; font-size: 0.88rem;
}
pre.cmdblock {
  background: #1c2530; color: #eaf1ff; padding: 12px 14px; border-radius: 8px;
  overflow-x: auto; font-size: 0.88rem;
}
pre.graph {
  background: #10161d; color: #d8e3f0; padding: 12px 14px; border-radius: 8px;
  overflow-x: auto; font-size: 0.82rem; white-space: pre; max-height: 420px;
}
label { display: block; font-weight: 600; font-size: 0.88rem; margin: 10px 0 4px; }
input[type=text], input[type=password] {
  width: 100%; padding: 8px 10px; border: 1px solid var(--border); border-radius: 6px;
  font-size: 0.95rem;
}
input[type=checkbox] { margin-right: 6px; }
button, .btn {
  background: var(--accent); color: #fff; border: none; padding: 9px 16px;
  border-radius: 7px; font-size: 0.92rem; cursor: pointer; font-weight: 600;
  text-decoration: none; display: inline-block; margin-top: 10px;
}
button:hover, .btn:hover { background: var(--accent-dark); }
button.secondary, .btn.secondary { background: var(--gray); }
button.danger, .btn.danger { background: var(--red); }
.row { display: flex; gap: 10px; flex-wrap: wrap; }
.badge {
  display: inline-block; border-radius: 999px; padding: 3px 11px; font-size: 0.78rem;
  font-weight: 700; letter-spacing: 0.02em;
}
.badge.green { background: var(--green-bg); color: var(--green); }
.badge.blue { background: var(--blue-bg); color: var(--accent-dark); }
.badge.orange { background: var(--orange-bg); color: var(--orange); }
.badge.red { background: var(--red-bg); color: var(--red); }
.badge.gray { background: var(--gray-bg); color: var(--gray); }
table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border); }
th { color: var(--muted); font-weight: 600; font-size: 0.8rem; text-transform: uppercase; }
.map { display: flex; flex-direction: column; align-items: center; gap: 2px; margin: 10px 0 20px; }
.map .node {
  border: 2px solid var(--border); border-radius: 10px; padding: 12px 18px;
  min-width: 320px; max-width: 640px; text-align: center; background: #fafcff;
}
.map .node.here { border-color: var(--accent); background: var(--blue-bg); }
.map .node .you-are-here {
  display: inline-block; background: var(--accent); color: #fff; font-size: 0.72rem;
  font-weight: 700; border-radius: 999px; padding: 2px 10px; margin-bottom: 6px;
  letter-spacing: 0.04em;
}
.map .arrow { font-size: 1.4rem; color: var(--muted); line-height: 1; }
.map .arrow .label { font-size: 0.75rem; display: block; color: var(--muted); }
.small-path { word-break: break-all; font-size: 0.82rem; color: var(--muted); }
.checklist-fail { color: var(--red); font-weight: 700; }
.checklist-pass { color: var(--green); font-weight: 700; }
.override { color: var(--orange); font-weight: 600; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
@media (max-width: 800px) { .grid2 { grid-template-columns: 1fr; } }
"""


def page(title: str, body: str, state: AppState | None = None) -> bytes:
    flash_html = ""
    if state is not None:
        message, kind = state.pop_flash()
        if message:
            flash_html = f'<div class="flash {esc(kind)}">{message}</div>'
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} - {APP_TITLE}</title>
<style>{CSS}</style>
</head>
<body>
<header class="top">
  <a href="/">{APP_TITLE}</a>
  <span class="muted" style="color:#c9d4e0">Local only - 127.0.0.1</span>
</header>
<main>
{flash_html}
{body}
</main>
</body>
</html>"""
    return html.encode("utf-8")


def hidden(name: str, value: str) -> str:
    return f'<input type="hidden" name="{esc(name)}" value="{esc(value)}">'


def command_block(commands: list[str]) -> str:
    lines = "\n".join(esc(c) for c in commands)
    return f'<pre class="cmdblock">{lines}</pre>'


def back_link() -> str:
    return '<a class="btn secondary" href="/">&larr; Back to dashboard</a>'


def confirmation_page(
    state: AppState,
    title: str,
    explanation: str,
    commands: list[str],
    confirm_url: str,
    pending: wf.PendingAction,
    extra_notice: str = "",
) -> bytes:
    warning_html = ""
    if pending.warning:
        warning_html = f'<p class="override">&#9888; {esc(pending.warning)}</p>'
    body = f"""
<div class="card">
  <h2>{esc(title)}</h2>
  <p>{explanation}</p>
  {warning_html}
  {extra_notice}
  <h3>Exact command(s) that will run</h3>
  {command_block(commands)}
  <p class="muted">This preview is tied to the repository's current state. If anything
  changes before you confirm, the action will be cancelled automatically for safety.
  Confirmations expire after about 10 minutes and can only be used once.</p>
  <form method="post" action="{esc(confirm_url)}">
    {hidden('csrf', state.csrf_token)}
    {hidden('token', pending.token)}
    <button type="submit">Confirm and run</button>
    <a class="btn secondary" href="/">Cancel</a>
  </form>
</div>
"""
    return page(title, body, state)


def result_page(state: AppState, title: str, body_extra: str) -> bytes:
    body = f"""
<div class="card">
  <h2>{esc(title)}</h2>
  {body_extra}
  {back_link()}
</div>
"""
    return page(title, body, state)


def error_page(state: AppState, message: str) -> bytes:
    body = f"""
<div class="card">
  <h2>Could not complete that action</h2>
  <p class="checklist-fail">{esc(message)}</p>
  {back_link()}
</div>
"""
    return page("Error", body, state)


def command_output_block(result: gs.CommandResult) -> str:
    out = esc(result.stdout.strip()) if result.stdout.strip() else "(no output)"
    err = f'<h3>Standard error</h3><pre class="cmdblock">{esc(result.stderr.strip())}</pre>' if result.stderr.strip() else ""
    status_class = "checklist-pass" if result.success else "checklist-fail"
    status_text = "succeeded" if result.success else f"failed (exit code {result.returncode})"
    return f"""
<p><code class="cmd">{esc(result.display_command)}</code>
&mdash; <span class="{status_class}">{status_text}</span>
<span class="muted">({result.duration_seconds:.2f}s)</span></p>
<h3>Output</h3>
<pre class="cmdblock">{out}</pre>
{err}
"""


# ---------------------------------------------------------------------------
# Workspace map / branch overview / commit graph rendering
# ---------------------------------------------------------------------------

_STATUS_LABEL = {
    "synchronized": ("Synchronized", "green"),
    "ahead": ("Ahead of remote", "blue"),
    "behind": ("Behind remote", "orange"),
    "diverged": ("Diverged", "red"),
    "no-upstream": ("No upstream configured", "gray"),
}

_STATUS_MEANING = {
    "M": "modified",
    "A": "added",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "U": "unmerged (conflict)",
    "?": "untracked",
    " ": "",
}


def _arrow(sync_status: str) -> str:
    if sync_status == "synchronized":
        return '<div class="arrow">&#10003;<span class="label">up to date</span></div>'
    if sync_status == "ahead":
        return '<div class="arrow">&darr;<span class="label">push to send commits</span></div>'
    if sync_status == "behind":
        return '<div class="arrow">&uarr;<span class="label">pull to receive commits</span></div>'
    if sync_status == "diverged":
        return '<div class="arrow">&#8645;<span class="label">diverged - pull needed</span></div>'
    return '<div class="arrow">&#8213;<span class="label">no remote tracking</span></div>'


def render_workspace_map(status: gs.RepoStatus) -> str:
    import os

    project_name = esc(os.path.basename(status.repo_root.rstrip("/\\")) or status.repo_root)
    remote_line = f"Remote: {esc(status.remote)}" if status.remote else "No remote configured"

    branch_label = esc(status.branch) if status.branch else "(detached HEAD)"
    detached_html = ""
    if status.detached:
        detached_html = '<p class="checklist-fail">&#9888; Detached HEAD: you are not on a branch. Commits made now can be lost unless you create a branch.</p>'

    branch_node = f"""
<div class="node here">
  <span class="you-are-here">You are here</span>
  <h3>Current local branch: {branch_label}</h3>
  <p class="mono">{esc(status.head_short_hash)} &mdash; {esc(status.head_subject)}</p>
  {detached_html}
</div>
"""

    u = status.upstream
    label, color = _STATUS_LABEL[u.sync_status]
    if u.has_upstream:
        upstream_body = (
            f'<p>{esc(u.remote or "")}/{esc(u.remote_branch or "")} '
            f'&mdash; <span class="badge {color}">{esc(label)}</span></p>'
            f'<p class="muted">{u.ahead} ahead &middot; {u.behind} behind</p>'
        )
    else:
        upstream_body = f'<p><span class="badge {color}">{esc(label)}</span></p>'

    upstream_node = f"""
<div class="node">
  <h3>Tracked remote branch</h3>
  {upstream_body}
</div>
"""

    project_node = f"""
<div class="node">
  <h3>Project workspace: {project_name}</h3>
  <p class="small-path">{esc(status.repo_root)}</p>
  <p class="muted">{remote_line}</p>
</div>
"""

    return f"""
<div class="map">
  {project_node}
  <div class="arrow">&darr;</div>
  {branch_node}
  {_arrow(u.sync_status)}
  {upstream_node}
</div>
"""


def render_changed_files(status: gs.RepoStatus) -> str:
    if status.is_clean:
        return '<p class="checklist-pass">Working directory is clean &mdash; no changes.</p>'
    rows = []
    for f in status.changed_files:
        idx_meaning = _STATUS_MEANING.get(f.index_status, f.index_status)
        wt_meaning = _STATUS_MEANING.get(f.worktree_status, f.worktree_status)
        meaning = ", ".join(m for m in (idx_meaning, wt_meaning) if m) or f.code
        rows.append(
            f"<tr><td class='mono'>{esc(f.code)}</td><td>{esc(meaning)}</td>"
            f"<td class='mono'>{esc(f.path)}</td></tr>"
        )
    return f"""
<table>
<tr><th>Code</th><th>Meaning</th><th>Path</th></tr>
{''.join(rows)}
</table>
"""


def render_branch_overview(branches: list[gs.BranchInfo]) -> str:
    if not branches:
        return '<p class="muted">No branches found.</p>'
    rows = []
    for b in sorted(branches, key=lambda x: (x.is_remote, not x.is_current, x.name)):
        kind = "Remote" if b.is_remote else ("Current local" if b.is_current else "Local")
        upstream = esc(b.upstream) if b.upstream else '<span class="muted">none</span>'
        tracking = f"{b.ahead} ahead / {b.behind} behind" if b.upstream else ""
        name_html = f"<strong>{esc(b.name)}</strong>" if b.is_current else esc(b.name)
        rows.append(
            f"<tr><td>{kind}</td><td>{name_html}</td><td class='mono'>{esc(b.short_hash)}</td>"
            f"<td>{esc(b.subject)}</td><td>{upstream}</td><td class='muted'>{esc(tracking)}</td></tr>"
        )
    return f"""
<table>
<tr><th>Type</th><th>Branch</th><th>Commit</th><th>Latest message</th><th>Upstream</th><th>Tracking</th></tr>
{''.join(rows)}
</table>
"""


def render_commit_graph(lines: list[str]) -> str:
    if not lines:
        return '<p class="muted">No commits yet.</p>'
    escaped = "\n".join(esc(line) for line in lines)
    return f'<pre class="graph">{escaped}</pre>'


# ---------------------------------------------------------------------------
# Dashboard sections
# ---------------------------------------------------------------------------


def render_repo_picker(state: AppState) -> str:
    current = f"<p class='muted'>Currently open: <span class='mono'>{esc(state.repo_root)}</span></p>" if state.repo_root else ""
    return f"""
<div class="card">
  <h2>1. Select your project</h2>
  {current}
  <form method="post" action="/select-repo">
    {hidden('csrf', state.csrf_token)}
    <label for="path">Local folder path</label>
    <input type="text" id="path" name="path" placeholder="C:\\Users\\you\\projects\\my-app" value="{esc(state.repo_root or '')}">
    <button type="submit">Open project</button>
  </form>
  <p class="muted">The folder must exist and be part of a Git repository (it, or a parent
  folder, must have been set up with <code class="cmd">git init</code> or cloned).</p>
</div>
"""


def render_checks_section(state: AppState, status: gs.RepoStatus) -> str:
    checks = state.checks
    current = wf.checks_are_current(checks, status.repo_root)
    if checks is None:
        result_html = '<p class="muted">Checks have not been run yet.</p>'
    else:
        pieces = []
        if not current:
            pieces.append('<p class="override">Files changed since these checks ran &mdash; run them again.</p>')
        if checks.app_result is not None:
            pieces.append("<h3>Application validation command</h3>" + command_output_block(checks.app_result))
        if checks.test_result is not None:
            pieces.append("<h3>Test command</h3>" + command_output_block(checks.test_result))
        overall = "checklist-pass" if (checks.passed and current) else "checklist-fail"
        overall_text = "PASSED" if (checks.passed and current) else "NOT PASSING"
        pieces.insert(0, f'<p class="{overall}">Overall: {overall_text}</p>')
        result_html = "".join(pieces)
    return f"""
<div class="card">
  <h2>Project checks</h2>
  <p class="muted">Configure a command to validate the app and/or run its tests. Commands run
  from the repository root, exactly as typed, with no shell involved.</p>
  <form method="post" action="/checks/run">
    {hidden('csrf', state.csrf_token)}
    <label for="app_cmd">Application validation command (optional)</label>
    <input type="text" id="app_cmd" name="app_cmd" placeholder="python -m compileall ." value="{esc(state.app_command)}">
    <label for="test_cmd">Test command (optional)</label>
    <input type="text" id="test_cmd" name="test_cmd" placeholder="python -m pytest -q" value="{esc(state.test_command)}">
    <button type="submit">Run checks now</button>
  </form>
  {result_html}
</div>
"""


def render_start_work_section(state: AppState, status: gs.RepoStatus) -> str:
    remote = status.remote or ""
    remote_html = (
        f"""
<form method="post" action="/fetch/preview">
  {hidden('csrf', state.csrf_token)}
  <label for="remote">Remote to check</label>
  <input type="text" id="remote" name="remote" value="{esc(remote)}">
  <button type="submit">Check remote (git fetch)</button>
</form>
"""
        if remote
        else '<p class="muted">No remote configured, so there is nothing to fetch.</p>'
    )
    return f"""
<div class="card">
  <h2>Start new work</h2>
  <h3>Step 1: check the remote</h3>
  {remote_html}
  <h3>Step 2: create a branch</h3>
  <form method="post" action="/branch/preview">
    {hidden('csrf', state.csrf_token)}
    <label for="branch_name">New branch name</label>
    <input type="text" id="branch_name" name="branch_name" placeholder="feature/my-change">
    <button type="submit">Preview branch creation</button>
  </form>
  <p class="muted">Requires a clean working directory, and that your current branch is not
  behind its upstream.</p>
</div>
"""


def render_commit_section(state: AppState, status: gs.RepoStatus) -> str:
    if status.is_clean:
        return """
<div class="card">
  <h2>Review and commit changes</h2>
  <p class="checklist-pass">Nothing to commit &mdash; working directory is clean.</p>
</div>
"""
    allowed, reason = wf.can_commit(state.checks, status.repo_root, override=False)
    status_line = (
        f'<p class="checklist-pass">{esc(reason)}</p>' if allowed
        else f'<p class="checklist-fail">{esc(reason)}</p>'
    )
    diff_unstaged = esc(gs.diff_stat(status.repo_root, staged=False).strip() or "(none)")
    diff_staged = esc(gs.diff_stat(status.repo_root, staged=True).strip() or "(none)")
    return f"""
<div class="card">
  <h2>Review and commit changes</h2>
  <h3>git status --short</h3>
  {render_changed_files(status)}
  <h3>git diff --stat (unstaged)</h3>
  <pre class="cmdblock">{diff_unstaged}</pre>
  <h3>git diff --cached --stat (staged)</h3>
  <pre class="cmdblock">{diff_staged}</pre>
  {status_line}
  <form method="post" action="/commit/preview">
    {hidden('csrf', state.csrf_token)}
    <label for="message">Commit message (one line)</label>
    <input type="text" id="message" name="message" maxlength="500" placeholder="Fix login validation bug">
    <label><input type="checkbox" name="override" value="1"> Override checks (I understand the risk)</label>
    <button type="submit">Preview commit</button>
  </form>
</div>
"""


def render_push_section(state: AppState, status: gs.RepoStatus) -> str:
    if status.detached:
        return '<div class="card"><h2>Push commits</h2><p class="checklist-fail">You are in detached HEAD; switch to a branch first.</p></div>'
    if not status.is_clean:
        return '<div class="card"><h2>Push commits</h2><p class="muted">Commit your changes before pushing.</p></div>'
    if not status.remote:
        return '<div class="card"><h2>Push commits</h2><p class="muted">No remote configured.</p></div>'
    allowed, reason = wf.can_push(state.checks, status.repo_root, override=False)
    status_line = (
        f'<p class="checklist-pass">{esc(reason)}</p>' if allowed
        else f'<p class="checklist-fail">{esc(reason)}</p>'
    )
    return f"""
<div class="card">
  <h2>Push commits safely</h2>
  {status_line}
  <form method="post" action="/push/preview">
    {hidden('csrf', state.csrf_token)}
    <label><input type="checkbox" name="override" value="1"> Override checks (I understand the risk)</label>
    <button type="submit">Preview push</button>
  </form>
  <p class="muted">A fresh <code class="cmd">git fetch</code> always runs first; if the remote
  has new commits, the push is stopped automatically. Force-push is never offered.</p>
</div>
"""


def render_pull_section(state: AppState, status: gs.RepoStatus) -> str:
    if not status.upstream.has_upstream:
        return '<div class="card"><h2>Pull remote changes</h2><p class="muted">This branch has no upstream branch configured.</p></div>'
    if not status.is_clean:
        return '<div class="card"><h2>Pull remote changes</h2><p class="muted">Commit or clean up changes before pulling.</p></div>'
    return f"""
<div class="card">
  <h2>Pull remote changes</h2>
  <p class="muted">Merge keeps a merge commit in your history; rebase replays your commits on
  top of the remote's for a straighter history. Either can produce conflicts.</p>
  <form method="post" action="/pull/preview">
    {hidden('csrf', state.csrf_token)}
    <div class="row">
      <label><input type="radio" name="mode" value="merge" checked> Merge (git pull --no-rebase)</label>
      <label><input type="radio" name="mode" value="rebase"> Rebase (git pull --rebase)</label>
    </div>
    <button type="submit">Preview pull</button>
  </form>
</div>
"""


def render_conflict_section(state: AppState, status: gs.RepoStatus) -> str:
    report = wf.get_conflict_report(status.repo_root)
    if report.state is None:
        return ""
    files_html = "".join(f"<li class='mono'>{esc(f)}</li>" for f in report.files) or "<li class='muted'>(none reported)</li>"
    return f"""
<div class="card">
  <h2>&#9888; Understanding Git conflicts</h2>
  <p class="checklist-fail">Your repository is in the middle of a <strong>{esc(report.state)}</strong>.</p>
  <h3>Conflicted files</h3>
  <ul>{files_html}</ul>
  <h3>What the markers mean</h3>
  <p>Inside a conflicted file, Git marks the disagreement like this:</p>
  <pre class="cmdblock">&lt;&lt;&lt;&lt;&lt;&lt;&lt; HEAD
your current changes
=======
the incoming changes
&gt;&gt;&gt;&gt;&gt;&gt;&gt; other-branch</pre>
  <p><code class="cmd">&lt;&lt;&lt;&lt;&lt;&lt;&lt;</code> starts your version, <code class="cmd">=======</code>
  divides the two versions, and <code class="cmd">&gt;&gt;&gt;&gt;&gt;&gt;&gt;</code> ends the incoming version.</p>
  <h3>Resolution steps</h3>
  <ol>
    <li>Open each conflicted file listed above.</li>
    <li>Edit the file so it contains exactly what you want, and remove all three marker lines.</li>
    <li>Save the file, then stage it with <code class="cmd">git add &lt;file&gt;</code> (run this yourself in a terminal, or Claude/your editor's Git tools).</li>
    <li>Once every file is resolved and staged, continue with: <code class="cmd">{esc(report.continue_hint or '')}</code></li>
  </ol>
  <p>If you'd rather undo the whole operation and go back to where you started:</p>
  <form method="post" action="/abort/preview">
    {hidden('csrf', state.csrf_token)}
    {hidden('state', report.state)}
    <button type="submit" class="secondary">Preview abort ({esc(report.abort_command or '')})</button>
  </form>
</div>
"""


def render_undo_section(state: AppState, status: gs.RepoStatus) -> str:
    try:
        plan = wf.plan_undo_last_commit(status.repo_root)
    except wf.WorkflowError:
        return '<div class="card"><h2>Undo last commit or push</h2><p class="muted">No commits yet.</p></div>'
    kind_label = "soft reset (keeps changes staged)" if plan.kind == "soft_reset" else "revert (adds a new reversing commit)"
    return f"""
<div class="card">
  <h2>Undo last commit or push</h2>
  <p>Latest commit: <span class="mono">{esc(plan.commit_short)}</span> &mdash; {esc(plan.commit_subject)}</p>
  <p>Recommended approach: <strong>{esc(kind_label)}</strong></p>
  <p class="muted">{esc(plan.explanation)}</p>
  <form method="post" action="/undo/preview">
    {hidden('csrf', state.csrf_token)}
    <button type="submit" class="secondary">Preview undo</button>
  </form>
</div>
"""


def render_history_section(state: AppState) -> str:
    entries = state.history.recent(25)
    if not entries:
        return '<div class="card"><h2>Command history</h2><p class="muted">No commands run yet this session.</p></div>'
    rows = []
    for e in entries:
        status_class = "checklist-pass" if e.success else "checklist-fail"
        status_text = "OK" if e.success else "FAILED"
        rows.append(
            f"<tr><td class='muted'>{esc(e.timestamp)}</td><td class='mono'>{esc(e.command)}</td>"
            f"<td class='{status_class}'>{status_text}</td><td>{esc(e.result)}</td></tr>"
        )
    return f"""
<div class="card">
  <h2>Command history (this session)</h2>
  <table>
  <tr><th>Time</th><th>Command</th><th>Result</th><th>Details</th></tr>
  {''.join(rows)}
  </table>
</div>
"""


def render_dashboard(state: AppState) -> bytes:
    sections = [render_repo_picker(state)]
    if state.repo_root:
        try:
            status = gs.build_repo_status(state.repo_root)
            branches = gs.list_branches(state.repo_root)
            graph_lines = gs.commit_graph_lines(state.repo_root)
        except gs.GitError as exc:
            sections.append(f'<div class="card"><p class="checklist-fail">{esc(str(exc))}</p></div>')
            return page("Dashboard", "".join(sections), state)

        sections.append(f'<div class="card"><h2>You are here</h2>{render_workspace_map(status)}'
                         f'<p>{esc(status.relationship_plain_language)}</p></div>')
        sections.append(f'<div class="card"><h2>Repository status</h2>'
                         f'<p><strong>Path:</strong> <span class="mono">{esc(status.repo_root)}</span></p>'
                         f'<p><strong>Changed / untracked files:</strong> {len(status.changed_files)} '
                         f'({len(status.untracked_files)} untracked)</p>'
                         f'{render_changed_files(status)}</div>')
        sections.append(f'<div class="card"><h2>Branch overview</h2>{render_branch_overview(branches)}</div>')
        sections.append(f'<div class="card"><h2>Commit graph (latest 30)</h2>{render_commit_graph(graph_lines)}</div>')
        sections.append(render_conflict_section(state, status))
        sections.append(render_checks_section(state, status))
        sections.append(render_start_work_section(state, status))
        sections.append(render_commit_section(state, status))
        sections.append(render_push_section(state, status))
        sections.append(render_pull_section(state, status))
        sections.append(render_undo_section(state, status))
        sections.append(render_history_section(state))
    return page("Dashboard", "".join(sections), state)


# ---------------------------------------------------------------------------
# Request handling
# ---------------------------------------------------------------------------


class CsrfError(Exception):
    pass


def require_repo(state: AppState) -> str:
    if not state.repo_root:
        raise wf.WorkflowError("No project is open yet. Select a folder first.")
    return state.repo_root


class Handler(server.BaseHTTPRequestHandler):
    server_version = "GitLocalAssistant/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        sys.stderr.write(f"[git-local-assistant] {self.address_string()} - {format % args}\n")

    @property
    def state(self) -> AppState:
        return self.server.state  # type: ignore[attr-defined]

    # -- helpers -----------------------------------------------------

    def _send(self, status_code: int, body: bytes, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        parsed = parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
        return {k: v[0] for k, v in parsed.items()}

    def _check_csrf(self, form: dict[str, str]) -> None:
        token = form.get("csrf", "")
        if not token or not secrets.compare_digest(token, self.state.csrf_token):
            raise CsrfError()

    # -- dispatch ------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        path = urlsplit(self.path).path
        try:
            if path == "/" or path == "":
                self._send(200, render_dashboard(self.state))
                return
            if path == "/healthz":
                self._send(200, b"ok", "text/plain; charset=utf-8")
                return
            self._send(404, page("Not found", '<div class="card">Page not found.</div>', self.state))
        except Exception as exc:  # noqa: BLE001 - top-level safety net
            self._send(500, error_page(self.state, f"Unexpected error: {exc}"))

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        path = urlsplit(self.path).path
        try:
            form = self._read_form()
            self._check_csrf(form)
            handler = POST_ROUTES.get(path)
            if handler is None:
                self._send(404, page("Not found", '<div class="card">Unknown action.</div>', self.state))
                return
            handler(self, form)
        except CsrfError:
            self._send(
                403,
                error_page(
                    self.state,
                    "This form's security token was missing or invalid, so the request was "
                    "rejected. Please go back and try again.",
                ),
            )
        except (wf.WorkflowError, gs.GitError) as exc:
            self._send(200, error_page(self.state, str(exc)))
        except Exception as exc:  # noqa: BLE001 - top-level safety net
            self._send(500, error_page(self.state, f"Unexpected error: {exc}"))


# ---------------------------------------------------------------------------
# POST action handlers
# ---------------------------------------------------------------------------


def _post_select_repo(h: Handler, form: dict[str, str]) -> None:
    path = form.get("path", "")
    root = gs.resolve_repo_root(path)
    h.state.repo_root = root
    h.state.checks = None
    h.state.set_flash(f"Opened project at <span class='mono'>{esc(root)}</span>.", "success")
    h._redirect("/")


def _post_checks_run(h: Handler, form: dict[str, str]) -> None:
    root = require_repo(h.state)
    h.state.app_command = form.get("app_cmd", "").strip()
    h.state.test_command = form.get("test_cmd", "").strip()
    h.state.checks = wf.run_checks(root, h.state.app_command, h.state.test_command, h.state.history)
    kind = "success" if h.state.checks.passed else "error"
    msg = "Checks passed." if h.state.checks.passed else f"Checks failed ({h.state.checks.failed_which})."
    h.state.set_flash(esc(msg), kind)
    h._redirect("/")


def _post_fetch_preview(h: Handler, form: dict[str, str]) -> None:
    root = require_repo(h.state)
    remote = form.get("remote", "").strip() or (gs.choose_default_remote(root) or "")
    if not remote:
        raise wf.WorkflowError("No remote configured for this repository.")
    pending = wf.preview_fetch(root, remote, h.state.confirmations)
    h._send(
        200,
        confirmation_page(
            h.state,
            "Check the remote",
            f"This will contact <strong>{esc(remote)}</strong> and download information about "
            "new commits, without changing any of your files or branches.",
            pending.commands,
            "/fetch/confirm",
            pending,
        ),
    )


def _post_fetch_confirm(h: Handler, form: dict[str, str]) -> None:
    token = form.get("token", "")
    pending = h.state.confirmations.consume(token, "fetch")
    result = wf.execute_fetch(pending, h.state.history)
    body = command_output_block(result) + "<p>The remote has been checked. Refresh the dashboard to see updated ahead/behind counts.</p>"
    h._send(200, result_page(h.state, "Remote checked", body))


def _post_branch_preview(h: Handler, form: dict[str, str]) -> None:
    root = require_repo(h.state)
    name = form.get("branch_name", "")
    pending = wf.preview_create_branch(root, name, h.state.confirmations)
    h._send(
        200,
        confirmation_page(
            h.state,
            "Create a new branch",
            f"This creates and switches to a new branch named <strong>{esc(pending.params['branch_name'])}</strong>, "
            "starting from your current commit.",
            pending.commands,
            "/branch/confirm",
            pending,
        ),
    )


def _post_branch_confirm(h: Handler, form: dict[str, str]) -> None:
    token = form.get("token", "")
    pending = h.state.confirmations.consume(token, "create_branch")
    result = wf.execute_create_branch(pending, h.state.history)
    body = command_output_block(result) + f"<p class='checklist-pass'>Now on branch {esc(pending.params['branch_name'])}.</p>"
    h._send(200, result_page(h.state, "Branch created", body))


def _post_commit_preview(h: Handler, form: dict[str, str]) -> None:
    root = require_repo(h.state)
    message = form.get("message", "")
    override = form.get("override") == "1"
    pending = wf.preview_commit(root, message, h.state.checks, override, h.state.confirmations)
    h._send(
        200,
        confirmation_page(
            h.state,
            "Commit changes",
            "This stages <strong>all</strong> current changes (git add --all) and creates one "
            "commit with your message.",
            pending.commands,
            "/commit/confirm",
            pending,
        ),
    )


def _post_commit_confirm(h: Handler, form: dict[str, str]) -> None:
    token = form.get("token", "")
    pending = h.state.confirmations.consume(token, "commit")
    results = wf.execute_commit(pending, h.state.history)
    new_hash = gs.get_head_short_hash(pending.repo_root)
    body = "".join(command_output_block(r) for r in results)
    body += (
        f"<h3>Rollback instructions</h3>"
        f"<p>This commit is <span class='mono'>{esc(new_hash)}</span>. As long as it is not yet "
        f"pushed, you can undo it while keeping your file changes staged, with:</p>"
        f"<pre class='cmdblock'>git reset --soft {esc(new_hash)}^</pre>"
        f"<p class='muted'>Or use the \"Undo last commit or push\" section on the dashboard, which "
        f"picks the safe option automatically.</p>"
    )
    h._send(200, result_page(h.state, "Commit created", body))


def _post_push_preview(h: Handler, form: dict[str, str]) -> None:
    root = require_repo(h.state)
    override = form.get("override") == "1"
    pending = wf.preview_push(root, h.state.checks, override, h.state.confirmations)
    notice = ""
    if pending.params.get("set_upstream"):
        notice = "<p class='muted'>No upstream is configured yet, so this push will also set one.</p>"
    h._send(
        200,
        confirmation_page(
            h.state,
            "Push commits",
            f"This re-checks <strong>{esc(pending.params['remote'])}</strong> for new commits, then "
            f"pushes branch <strong>{esc(pending.params['branch'])}</strong> if it is safe to do so.",
            pending.commands,
            "/push/confirm",
            pending,
            extra_notice=notice,
        ),
    )


def _post_push_confirm(h: Handler, form: dict[str, str]) -> None:
    token = form.get("token", "")
    pending = h.state.confirmations.consume(token, "push")
    outcome = wf.execute_push(pending, h.state.history)
    body = command_output_block(outcome["fetch"]) + command_output_block(outcome["push"])
    hashes = outcome.get("pushed_commits") or []
    if hashes:
        revert_cmds = "\n".join(f"git revert {esc(hh[:10])}" for hh in hashes)
        body += (
            f"<h3>Commits pushed ({len(hashes)})</h3>"
            f"<pre class='cmdblock'>{chr(10).join(esc(hh) for hh in hashes)}</pre>"
            f"<h3>Rollback instructions (if needed)</h3>"
            f"<p>These are already on the remote, so history must not be rewritten. To undo them, "
            f"revert newest-first with:</p>"
            f"<pre class='cmdblock'>{revert_cmds}</pre>"
            f"<p class='muted'>Run your project checks again after reverting, then push the revert "
            f"commit(s) normally. If any secret was pushed, rotate/revoke it immediately &mdash; "
            f"reverting does not remove it from history.</p>"
        )
    h._send(200, result_page(h.state, "Push complete", body))


def _post_pull_preview(h: Handler, form: dict[str, str]) -> None:
    root = require_repo(h.state)
    mode = form.get("mode", "merge")
    pending = wf.preview_pull(root, mode, h.state.confirmations)
    explanation = (
        "This merges the remote branch into yours, creating a merge commit if needed."
        if mode == "merge"
        else "This replays your local commits on top of the remote branch's latest commits."
    )
    h._send(
        200,
        confirmation_page(h.state, "Pull remote changes", explanation, pending.commands, "/pull/confirm", pending),
    )


def _post_pull_confirm(h: Handler, form: dict[str, str]) -> None:
    token = form.get("token", "")
    pending = h.state.confirmations.consume(token, "pull")
    result = wf.execute_pull(pending, h.state.history)
    body = command_output_block(result)
    if not result.success:
        state_now = gs.detect_special_state(pending.repo_root)
        if state_now:
            body += (
                f"<p class='checklist-fail'>Git reported a conflict during this {esc(state_now)}. "
                "See the conflict section on the dashboard for guided resolution steps.</p>"
            )
    h._send(200, result_page(h.state, "Pull finished", body))


def _post_abort_preview(h: Handler, form: dict[str, str]) -> None:
    root = require_repo(h.state)
    state_name = form.get("state", "")
    pending = wf.preview_abort(root, state_name, h.state.confirmations)
    h._send(
        200,
        confirmation_page(
            h.state,
            f"Abort {esc(state_name)}",
            "This returns the repository to how it was before the operation started. "
            "Any partial resolution you have made to conflicted files will be discarded.",
            pending.commands,
            "/abort/confirm",
            pending,
        ),
    )


def _post_abort_confirm(h: Handler, form: dict[str, str]) -> None:
    token = form.get("token", "")
    pending = h.state.confirmations.consume(token, "abort")
    result = wf.execute_abort(pending, h.state.history)
    body = command_output_block(result)
    h._send(200, result_page(h.state, "Operation aborted", body))


def _post_undo_preview(h: Handler, form: dict[str, str]) -> None:
    root = require_repo(h.state)
    plan, pending = wf.preview_undo_last_commit(root, h.state.confirmations)
    if plan.kind == "soft_reset":
        explanation = (
            f"Commit <span class='mono'>{esc(plan.commit_short)}</span> has not been pushed. It will "
            "be removed from history, but its file changes will remain staged so no work is lost."
        )
    else:
        explanation = (
            f"Commit <span class='mono'>{esc(plan.commit_short)}</span> already exists on the remote. "
            "Git will create a new commit that reverses its changes, rather than rewriting shared history."
        )
    h._send(
        200,
        confirmation_page(h.state, "Undo last commit", explanation, pending.commands, "/undo/confirm", pending),
    )


def _post_undo_confirm(h: Handler, form: dict[str, str]) -> None:
    token = form.get("token", "")
    pending = h.state.confirmations.consume(token, "undo_last_commit")
    result = wf.execute_undo_last_commit(pending, h.state.history)
    body = command_output_block(result)
    if pending.params["kind"] == "revert":
        body += "<p class='muted'>Run your project checks again, then push this revert commit normally.</p>"
    h._send(200, result_page(h.state, "Undo complete", body))


POST_ROUTES: dict[str, Callable[[Handler, dict[str, str]], None]] = {
    "/select-repo": _post_select_repo,
    "/checks/run": _post_checks_run,
    "/fetch/preview": _post_fetch_preview,
    "/fetch/confirm": _post_fetch_confirm,
    "/branch/preview": _post_branch_preview,
    "/branch/confirm": _post_branch_confirm,
    "/commit/preview": _post_commit_preview,
    "/commit/confirm": _post_commit_confirm,
    "/push/preview": _post_push_preview,
    "/push/confirm": _post_push_confirm,
    "/pull/preview": _post_pull_preview,
    "/pull/confirm": _post_pull_confirm,
    "/abort/preview": _post_abort_preview,
    "/abort/confirm": _post_abort_confirm,
    "/undo/preview": _post_undo_preview,
    "/undo/confirm": _post_undo_confirm,
}


# ---------------------------------------------------------------------------
# Server bootstrap
# ---------------------------------------------------------------------------


class LocalHTTPServer(server.ThreadingHTTPServer):
    """A ThreadingHTTPServer that refuses to share its port with another
    instance, on every supported platform."""

    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self) -> None:
        if sys.platform == "win32":
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)  # type: ignore[attr-defined]
            except (AttributeError, OSError):
                pass
        super().server_bind()


def create_server(port: int, repo: str | None) -> LocalHTTPServer:
    state = AppState()
    if repo:
        try:
            state.repo_root = gs.resolve_repo_root(repo)
        except gs.GitError as exc:
            sys.stderr.write(f"[git-local-assistant] --repo ignored: {exc}\n")
    httpd = LocalHTTPServer(("127.0.0.1", port), Handler)
    httpd.state = state  # type: ignore[attr-defined]
    return httpd

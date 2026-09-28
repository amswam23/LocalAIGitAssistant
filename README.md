# Git Local Assistant

A local workspace, beginner-friendly browser dashboard that helps a single developer
safely manage Git operations on a local project — **without ever hiding what
Git is actually doing.**

Every action the tool can take shows you the exact Git command(s) it is
about to run, in a plain-English preview, before it runs them. Nothing is
executed silently.

## Product description

Git is powerful but unforgiving, and its command-line messages are written
for people who already know Git. Git Local Assistant sits on top of your
existing local Git installation and turns common, everyday workflows into a
guided, readable dashboard: what state is my project in, what will happen if
I click this button, and what do I do if something goes wrong?

It is a small, self-contained Python application. It has no database, needs
no separate backend framework, and only talks to `127.0.0.1` — nothing you
do in it is ever sent anywhere else. It runs the real `git` executable
already installed on your machine, using your existing Git configuration,
SSH keys, and credential manager.

## Feature list

- **Workspace map** — a "You are here" diagram showing your project folder,
  current branch, and tracked remote branch, with clear ahead/behind/
  diverged/synchronized status.
- **Branch overview** — every local and remote branch, with commit, upstream,
  and tracking information.
- **Commit graph** — a compact, monospace `git log --graph` view of the
  latest commits.
- **Check the remote** (`git fetch`) before starting new work.
- **Create a branch** (`git switch -c <name>`), blocked from a dirty or
  behind-upstream base branch.
- **Project checks** — configure an application-validation command and a
  test command; both run with `shell=False`, from the repository root.
- **Review and commit changes** — see `git status --short` and diff stats,
  write a one-line message, and preview the exact `git add --all` /
  `git commit` commands before they run. Commits are blocked until checks
  pass, unless you explicitly override.
- **Push safely** — always re-fetches first; stops automatically (no
  force-push, ever) if the remote has moved on; sets upstream automatically
  the first time.
- **Pull remote changes** — choose merge or rebase, with plain-language
  explanations of the difference.
- **Conflict guidance** — detects merge/rebase/revert/cherry-pick states,
  lists conflicted files, explains the `<<<<<<<` / `=======` / `>>>>>>>`
  markers, gives numbered resolution steps, and always offers the correct
  abort command.
- **Rollback help** — "Undo last commit or push" automatically figures out
  whether your last commit is only local (safe to `git reset --soft`) or
  already shared (needs a `git revert`), and previews the right command.
- **Command history** for the current session — timestamp, exact command,
  success/failure, and a short result summary.
- **Two-step confirmation on everything that changes the repository** —
  every state-changing action is previewed, tied to a single-use token and a
  snapshot ("fingerprint") of the repository, and is cancelled automatically
  if the repository changes before you confirm.
- **No dangerous operations** — `git reset --hard`, force-push, and branch
  deletion are not offered as normal buttons in this MVP.

## Requirements

- Python 3.10 or newer.
- Git installed and available on your `PATH`.
- Windows, macOS, or Linux (Windows is the primary target; macOS/Linux are
  supported using the same Python entry points).
- No Flask, Django, FastAPI, Node.js, or database required — the UI is
  served by Python's standard-library HTTP server.

## Windows + VS Code: quick start

```powershell
cd <project-location>\git-local-assistant
code .
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest -q
git-local-assistant
```

### F5 launch (VS Code)

1. Open the folder in VS Code (`code .`).
2. Select the `.venv` interpreter if prompted (or via
   `Ctrl+Shift+P` → *Python: Select Interpreter*).
3. Press **F5** and choose **"Git Local Assistant"**.

The `.vscode/launch.json` file included in this project already defines
three configurations: the app itself, the app on a custom port without
auto-opening a browser, and the test suite.

### Zero-install launcher (no manual venv steps)

If you don't want to run the venv commands yourself, use the included
PowerShell launcher. It creates a virtual environment on its first run,
installs the project into it, and starts the app — all in one step:

```powershell
.\run.ps1
```

Arguments are passed straight through, e.g.:

```powershell
.\run.ps1 --port 8080 --repo C:\Users\me\projects\my-app
```

### Manual virtual environment setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

On macOS/Linux, activate with `source .venv/bin/activate` instead.

`pip install -e ".[dev]"` is an **editable install**: the `git-local-assistant`
command and `python -m git_local_assistant` both run directly from the
`src/` folder, so any changes you make take effect immediately without
reinstalling.

### Running the tests

```powershell
python -m pytest -q
python -m compileall -q src
python -m ruff check src tests   # optional, if Ruff is installed
```

### Normal startup

```powershell
git-local-assistant
```

This starts the server on `http://127.0.0.1:7332/` and opens your default
browser automatically. Stop it with `Ctrl+C`.

You can also start it as a module, which works identically and needs no
installed console script:

```powershell
python -m git_local_assistant
```

Command-line options:

| Option         | Meaning                                             |
|----------------|------------------------------------------------------|
| `--port PORT`  | Port to listen on (default: `7332`).                 |
| `--repo PATH`  | Open this local Git repository immediately on start. |
| `--no-browser` | Don't open a browser window automatically.            |

### `--repo` example

```powershell
git-local-assistant --repo C:\Users\me\projects\my-app
```

## Try it safely first: a disposable practice repository

Before pointing this at a real project, it's worth trying the workflows on
a throwaway repository you can delete afterwards:

```powershell
mkdir $env:TEMP\git-local-assistant-playground
cd $env:TEMP\git-local-assistant-playground
git init
git config user.email "you@example.com"
git config user.name "Your Name"
"hello" | Out-File -Encoding utf8 readme.txt
git add .
git commit -m "initial commit"
```

Then run `git-local-assistant --repo $env:TEMP\git-local-assistant-playground`
and try each dashboard section: edit `readme.txt`, run checks, commit,
create a branch, and (if you also `git remote add origin <any bare repo>`)
try fetch/push/pull. When you're done, just delete the folder — nothing
here touches anything outside it.

## Credential handling

Git Local Assistant never asks for, stores, or transmits Git passwords,
personal access tokens, or SSH keys. All authentication for `fetch`,
`pull`, and `push` is handled entirely by your existing Git setup — the
Git Credential Manager, an SSH agent, or however you've already configured
Git on this machine. This also means the tool works the same way with
GitHub, GitLab, Bitbucket, Azure DevOps, self-hosted servers, or local bare
repositories: it never implements provider-specific authentication.

## Rollback explanation

- **Unpushed commit:** the default is `git reset --soft <hash>^`, which
  removes the commit but keeps its file changes staged — nothing is
  discarded. `git reset --hard` is never used as the default rollback.
- **Already-pushed commit:** the tool uses `git revert <hash>`, which adds
  a *new* commit that undoes the change, rather than rewriting shared
  history.
- **Multiple pushed commits:** after a push, the exact commit hashes are
  shown along with ready-to-run `git revert` commands, to be applied
  newest-first.
- **"Undo last commit or push"** automatically checks (via
  `git merge-base --is-ancestor`) whether your last commit is already on
  the upstream branch, and previews the soft-reset or the revert
  accordingly — it never guesses, and never auto-pushes a revert for you.
- **If a secret was accidentally committed and pushed:** reverting is
  **not enough**, since the secret still exists in Git history. Rotate or
  revoke the exposed credential immediately. This tool does not attempt to
  rewrite history automatically.

## Known MVP limitations

- One selected project per application session.
- Settings and command history are kept only in memory for the current
  session; nothing persists after you stop the app.
- Conflicts are explained and can be aborted, but files are never edited
  automatically — you resolve conflict markers yourself.
- Commits always stage everything (`git add --all`); there is no partial /
  interactive staging in this MVP.
- Intended for localhost use only; it is not designed to be exposed on a
  network.
- There is no background filesystem watcher — the dashboard reflects the
  repository's real state whenever you load or reload the page, and always
  refreshes after any action the tool performs itself.
- Dangerous operations (`git reset --hard`, force-push, branch deletion) are
  intentionally not implemented as normal buttons in this MVP.

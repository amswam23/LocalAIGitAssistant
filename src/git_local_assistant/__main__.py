"""Command-line entry point for Git Local Assistant."""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser

from . import git_service as gs
from .app import create_server

DEFAULT_PORT = 7332


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="git-local-assistant",
        description=(
            "A local, beginner-friendly browser dashboard for safely managing Git "
            "operations on a single local repository."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Port to listen on (default: {DEFAULT_PORT}).",
    )
    parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help="Path to a local Git repository to open immediately.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not automatically open a browser window on startup.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not gs.git_available():
        print(
            "Git does not appear to be installed, or is not on your PATH.\n"
            "Install Git from https://git-scm.com/downloads and try again.",
            file=sys.stderr,
        )
        return 1

    try:
        httpd = create_server(args.port, args.repo)
    except OSError as exc:
        print(
            f"Could not start the server on 127.0.0.1:{args.port} ({exc}).\n"
            "Another instance of Git Local Assistant may already be running on this "
            "port, or another program is using it. Try a different port with "
            "--port, or close the other instance.",
            file=sys.stderr,
        )
        return 1

    url = f"http://127.0.0.1:{args.port}/"
    print(f"Git Local Assistant is running at {url}")
    print("Press Ctrl+C to stop.")

    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Git Local Assistant...")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

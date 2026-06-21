"""Command-line entry point for deja-func.

For M1 this is intentionally tiny: a CLI that installs cleanly and proves the
plumbing works (`deja --version`, `deja hello`). The real commands (`index`,
`find`, ...) arrive in later milestones — see PLAN.md §7.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__

# A little personality, per PLAN.md §4.
HELLO_MESSAGE = (
    "🫠 deja-func is awake.\n"
    "Soon I'll remember every function you've written so you stop rewriting "
    "slugify for the fourth time.\n"
    "Try `deja --version` for now — `index` and `find` are on the way."
)


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="deja",
        description="Index your functions so you (and your AI agents) stop reinventing them.",
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"deja-func {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    hello = subparsers.add_parser("hello", help="Print a friendly hello and confirm install.")
    hello.set_defaults(func=cmd_hello)

    return parser


def cmd_hello(_args: argparse.Namespace) -> int:
    """Handle `deja hello`."""
    print(HELLO_MESSAGE)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Program entry point. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    func = getattr(args, "func", None)
    if func is None:
        # No subcommand given: show help, but don't treat it as a hard error.
        parser.print_help()
        return 0

    return func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

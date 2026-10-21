"""GUI entry point: python -m dispatch"""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Allow `python -m dispatch validate ...` to behave like the CLI.
    if argv and argv[0] in {"validate", "send", "export", "sync-inbox", "flush", "init",
                            "template", "--version", "-h", "--help"}:
        from .cli import main as cli_main
        return cli_main(argv)

    from .ui.app import run
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())

"""Entry point for the packaged worker.

PyInstaller runs its entry script as a top-level module, not as part of the
package, so this file uses absolute imports. ``python -m process_studio.worker``
still goes through ``__main__``.
"""

from __future__ import annotations

import sys

from process_studio.worker.protocol import serve


def main() -> int:
    # The shell starts the worker without arguments and talks RPC over stdio.
    # With arguments the same binary is the command-line tool, so a packaged
    # build carries the CLI without a second executable.
    if len(sys.argv) > 1:
        from process_studio.cli import main as cli_main

        return cli_main(sys.argv[1:])
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())

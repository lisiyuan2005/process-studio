"""Entry point for the packaged worker.

PyInstaller runs its entry script as a top-level module, not as part of the
package, so this file uses absolute imports. ``python -m process_studio.worker``
still goes through ``__main__``.
"""

from __future__ import annotations

from process_studio.worker.protocol import serve


def main() -> int:
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())

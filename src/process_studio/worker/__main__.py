import sys

from .protocol import serve


def main() -> int:
    return serve()


if __name__ == "__main__":
    sys.exit(main())

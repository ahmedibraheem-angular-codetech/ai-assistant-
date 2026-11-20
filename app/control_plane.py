"""Token commands for the local API.

    python -m app.control_plane token    # print the current token
    python -m app.control_plane rotate   # new token; the server must be stopped
    python -m app.control_plane path     # where the token file lives
"""

from __future__ import annotations

import sys

from app.core.auth import TOKEN_NAME, AuthStoreError, ServerRunningError, data_dir, read_token, rotate


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    command = args[0] if args else ""
    try:
        if command == "token":
            print(read_token())
            return 0
        if command == "path":
            print(data_dir() / TOKEN_NAME)
            return 0
        if command == "rotate":
            token = rotate()
            print("New token saved. Start the server to use it.", file=sys.stderr)
            if sys.stdout.isatty():
                print(token)
            return 0
    except ServerRunningError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except AuthStoreError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(__doc__.strip(), file=sys.stderr)
    return 64


if __name__ == "__main__":
    raise SystemExit(main())

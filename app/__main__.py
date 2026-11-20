"""Start the API on localhost: python -m app [--port 8000]"""

import argparse

import uvicorn

from app.core.config import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    host = get_settings().api_host.strip() or "127.0.0.1"
    uvicorn.run("app.main:app", host=host, port=args.port, workers=1, proxy_headers=False)


if __name__ == "__main__":
    main()

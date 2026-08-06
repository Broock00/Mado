"""Development server entry point.

Starts uvicorn on an explicitly-constructed event loop rather than letting it pick
one, because the database driver requires a selector loop on Windows (see
:mod:`app.core.asyncio_compat`).

For production the process manager runs uvicorn directly against ``app.main:app``
on Linux, where no such constraint applies.
"""

from __future__ import annotations

import argparse

import uvicorn

from app.core.asyncio_compat import run as run_async


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Mado API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    config = uvicorn.Config(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_config=None,  # structlog owns formatting
    )
    server = uvicorn.Server(config)
    run_async(server.serve())


if __name__ == "__main__":
    main()

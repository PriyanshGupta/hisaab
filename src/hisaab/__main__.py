"""`python -m hisaab` starts the server; `python -m hisaab <command>` runs a CLI command (see cli.py)."""

import sys

import uvicorn

from hisaab import cli
from hisaab.config import Settings


def serve() -> None:
    settings: Settings = Settings()
    uvicorn.run("hisaab.main:create_app", factory=True, host=settings.host, port=settings.port)


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        serve()
        return
    sys.exit(cli.main(argv))


if __name__ == "__main__":
    main()

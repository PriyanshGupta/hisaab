"""`python -m hisaab` starts the server on the host/port from Settings (.env)."""

import uvicorn

from hisaab.config import Settings


def main() -> None:
    settings = Settings()
    uvicorn.run("hisaab.main:create_app", factory=True, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()

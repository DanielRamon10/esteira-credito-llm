"""Entrypoint do servico."""

from __future__ import annotations

import uvicorn

from customer_support.api.app import criar_app
from customer_support.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(criar_app(settings), host=settings.host, port=settings.porta, log_config=None)


if __name__ == "__main__":
    main()

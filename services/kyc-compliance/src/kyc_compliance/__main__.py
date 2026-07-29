"""Entrypoint do servico.

`python -m kyc_compliance`, no mesmo padrao do outro servico. Aqui nao ha o
problema de event loop do psycopg no Windows (este servico nao usa Postgres), mas
manter o mesmo comando entre os dois elimina uma pegadinha de operacao: quem sabe
subir um sabe subir o outro.
"""

from __future__ import annotations

import uvicorn

from kyc_compliance.api.app import criar_app
from kyc_compliance.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        criar_app(settings),
        host=settings.host,
        port=settings.porta,
        log_config=None,  # o structlog ja configurou a stdlib
    )


if __name__ == "__main__":
    main()

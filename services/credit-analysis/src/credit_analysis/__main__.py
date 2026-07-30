"""Ponto de entrada do servico: `python -m credit_analysis`.

Existe por um motivo concreto. No Windows, `uvicorn.run()` chama internamente

    asyncio_run(server.serve(), loop_factory=ProactorEventLoop)

com a fabrica **fixa no codigo** (`uvicorn/loops/asyncio.py`). Como o
`loop_factory` tem precedencia sobre a policy do processo, ajustar a policy
antes nao muda nada — foi o que aconteceu na primeira tentativa. E o psycopg
async nao roda sobre Proactor, entao o servidor trava em "Waiting for
application startup" sem erro visivel.

A saida e nao usar `uvicorn.run()`: montamos o `Server` e o executamos com o
loop correto para a plataforma.

Em Linux (o container de producao) `fabrica_de_loop()` devolve None e isto vira
um `asyncio.run` comum — nenhum comportamento especial de producao depende
deste modulo.
"""

from __future__ import annotations

import uvicorn

from credit_analysis.config import get_settings
from credit_analysis.infrastructure.event_loop import executar


def main() -> None:
    settings = get_settings()

    config = uvicorn.Config(
        # `criar_app` e nao `app`, com `factory=True`: a aplicacao passa a ser construida
        # quando o servidor sobe, e nao no import do modulo. Ver a nota no fim de
        # `api/app.py`.
        "credit_analysis.api.app:criar_app",
        factory=True,
        host=settings.host,
        port=settings.porta,
        log_config=None,  # structlog ja configura o logging
    )
    executar(uvicorn.Server(config).serve())


if __name__ == "__main__":
    main()

"""Compatibilidade de event loop no Windows.

O psycopg em modo async nao funciona sobre o `ProactorEventLoop`, que e o
padrao do Python no Windows desde a 3.8. Sem isto, qualquer conexao com o
Postgres falha com:

    Psycopg cannot use the 'ProactorEventLoop' to run in async mode

O Proactor e baseado em IOCP e nao expoe a interface `add_reader`/`add_writer`
que o psycopg usa para aguardar o socket. O `SelectorEventLoop` expoe, e por
isso e o loop compativel.

Em Linux — onde o servico roda em producao, dentro do container — o loop
padrao ja e baseado em selector e nada disto se aplica. O modulo existe para
que o ambiente de desenvolvimento no Windows se comporte como o de producao.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable

# Python 3.12+ aceita `loop_factory` em `asyncio.run`, que e preferivel a
# mexer na policy global: o efeito fica restrito a chamada.
FabricaDeLoop = Callable[[], asyncio.AbstractEventLoop] | None


def fabrica_de_loop() -> FabricaDeLoop:
    """Fabrica a passar para `asyncio.run(..., loop_factory=...)`.

    Devolve None fora do Windows, onde o padrao ja serve.
    """
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop
    return None


def executar(corotina: asyncio.Future[object] | object) -> object:
    """`asyncio.run` com o loop correto para a plataforma."""
    fabrica = fabrica_de_loop()
    if fabrica is None:
        return asyncio.run(corotina)  # type: ignore[arg-type]
    return asyncio.run(corotina, loop_factory=fabrica)  # type: ignore[arg-type]


def ajustar_policy_global() -> None:
    """Troca a policy do processo inteiro para SelectorEventLoop no Windows.

    Serve para quem cria o loop a partir da policy — o pytest-asyncio e o caso.

    **Nao funciona para o Uvicorn.** A partir da 0.36 ele passa um
    `loop_factory` fixo (`ProactorEventLoop` no Windows) para `asyncio.run`, e
    `loop_factory` tem precedencia sobre a policy. Por isso o servico tem um
    `__main__.py` que monta o `uvicorn.Server` e o executa via `executar()`.

    Prefira `executar()` quando voce controla a chamada; esta funcao tem efeito
    global e deve ficar restrita a pontos de entrada de processo.
    """
    if sys.platform != "win32":
        return

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

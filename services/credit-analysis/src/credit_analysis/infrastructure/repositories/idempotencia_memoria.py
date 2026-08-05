"""Registro de idempotencia em memoria, para desenvolvimento e teste.

## Por que ele existe, se em memoria nao serve para producao

Mesma razao do `RepositorioAnalisesMemoria`: a alternativa seria a rota responder 503 sem Postgres,
e ai **toda** a suite que exercita `POST /v1/analises` — 39 chamadas em 7 arquivos — passaria a
medir a recusa em vez do comportamento.

A garantia de que isto nao chega a producao e a mesma do repositorio, e ela e estrutural:
`criar_app` recusa subir em `prod` sem `CREDIT_POSTGRES_DSN`.

## A limitacao, dita com precisao

O registro e **do processo**. Com duas replicas de API, a primeira chamada reivindica a chave na
replica A e a repeticao que cair na B nao encontra nada — cria a segunda analise. Ou seja: em
memoria a idempotencia vale por replica, que e o mesmo que nao valer.

E por isso que a limitacao nao e "menos durabilidade": e ausencia da garantia. Em producao o
registro e o de Postgres.

## Por que a reivindicacao nao tem `await` no meio

O `asyncio` e cooperativo: entre dois `await` nada mais roda nesta thread. `reivindicar` consulta e
grava sem suspender, o que torna a operacao atomica **dentro de um processo** — e e a mesma
propriedade que o `INSERT ... ON CONFLICT` da no Postgres, pelo motivo oposto (la o banco serializa;
aqui ninguem interrompe).

Um `await` entre a consulta e a gravacao reintroduziria exatamente a janela que a Camada 11 existe
para fechar, e o teste de corrida pegaria.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from credit_analysis.domain.idempotencia import (
    JANELA,
    PRAZO_DE_ABANDONO,
    EstadoDaChave,
    RegistroDeIdempotencia,
)
from credit_analysis.infrastructure.repositories.idempotencia import Reivindicacao


class RegistroIdempotenciaMemoria:
    def __init__(self) -> None:
        self._chaves: dict[tuple[str, str], RegistroDeIdempotencia] = {}

    async def reivindicar(
        self, locatario: str, chave: str, impressao: str, agora: datetime
    ) -> Reivindicacao:
        indice = (locatario, chave)
        registro = self._chaves.get(indice)

        expirada = registro is not None and agora - registro.criada_em >= JANELA
        abandonada = (
            registro is not None
            and registro.estado is EstadoDaChave.EM_ANDAMENTO
            and agora - registro.criada_em >= PRAZO_DE_ABANDONO
        )

        if registro is None or expirada or abandonada:
            self._chaves[indice] = RegistroDeIdempotencia(
                chave=chave,
                locatario=locatario,
                impressao=impressao,
                estado=EstadoDaChave.EM_ANDAMENTO,
                recurso_id=None,
                criada_em=agora,
            )
            return Reivindicacao(reivindicada=True, registro=None)

        return Reivindicacao(reivindicada=False, registro=registro)

    async def concluir(self, locatario: str, chave: str, recurso_id: UUID) -> None:
        indice = (locatario, chave)
        registro = self._chaves.get(indice)
        if registro is None:
            return
        self._chaves[indice] = RegistroDeIdempotencia(
            chave=registro.chave,
            locatario=registro.locatario,
            impressao=registro.impressao,
            estado=EstadoDaChave.CONCLUIDA,
            recurso_id=recurso_id,
            criada_em=registro.criada_em,
        )

    async def liberar(self, locatario: str, chave: str) -> None:
        registro = self._chaves.get((locatario, chave))
        # A guarda de estado e a mesma do Postgres: um `liberar` chamado por engano no caminho de
        # sucesso apagaria a chave de um pedido concluido, e a repeticao criaria a segunda analise.
        if registro is not None and registro.estado is EstadoDaChave.EM_ANDAMENTO:
            del self._chaves[(locatario, chave)]

    async def purgar_vencidas(self, agora: datetime) -> int:
        vencidas = [
            indice
            for indice, registro in self._chaves.items()
            if agora - registro.criada_em >= JANELA
        ]
        for indice in vencidas:
            del self._chaves[indice]
        return len(vencidas)

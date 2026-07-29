"""Repositorio de triagens em memoria.

Postgres entra quando houver retencao real a atender — a Circular BCB 3.978 exige
cinco anos, e memoria nao atende isso. Fica explicito aqui em vez de prometido: o
port ja esta no lugar, e trocar o adapter e uma linha no composition root.
"""

from __future__ import annotations

from uuid import UUID

from kyc_compliance.domain.triagem import Triagem


class RepositorioTriagensMemoria:
    def __init__(self) -> None:
        self._por_id: dict[UUID, Triagem] = {}
        # Contador de insercao como desempate na ordenacao.
        #
        # O relogio do Windows tem resolucao de ~15ms: duas triagens gravadas em
        # sequencia compartilham `criada_em`, e uma ordenacao estavel devolveria a
        # ordem de insercao — o inverso de "mais recente primeiro". Este bug
        # apareceu de verdade no outro servico; aqui ja nasce resolvido.
        self._ordem: dict[UUID, int] = {}

    async def salvar(self, triagem: Triagem) -> None:
        if triagem.id not in self._ordem:
            self._ordem[triagem.id] = len(self._ordem)
        self._por_id[triagem.id] = triagem

    async def buscar_por_id(self, triagem_id: UUID) -> Triagem | None:
        return self._por_id.get(triagem_id)

    async def listar(self, limite: int = 50, offset: int = 0) -> list[Triagem]:
        ordenadas = sorted(
            self._por_id.values(),
            key=lambda t: (t.criada_em, self._ordem[t.id]),
            reverse=True,
        )
        return ordenadas[offset : offset + limite]

    async def contar(self) -> int:
        return len(self._por_id)

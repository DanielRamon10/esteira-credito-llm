"""Adapters de consulta a bureau de credito.

Na Camada 1 existe apenas o stub deterministico. O adapter HTTP real entra
junto com a observabilidade (Camada 5), quando houver timeout, retry com
backoff e circuit breaker para instrumentar.
"""

from __future__ import annotations

import hashlib


class BureauStub:
    """Bureau deterministico para desenvolvimento e teste.

    Deriva a resposta de um hash do CPF em vez de sortear: o mesmo CPF sempre
    devolve o mesmo resultado, entao um teste que passou nao quebra na proxima
    execucao. Aproximadamente 20% dos CPFs retornam com restricao.
    """

    def __init__(self, taxa_restricao: float = 0.20) -> None:
        if not 0.0 <= taxa_restricao <= 1.0:
            raise ValueError("taxa_restricao deve estar entre 0 e 1")
        self._corte = int(taxa_restricao * 256)

    async def tem_restricao(self, cpf: str) -> bool:
        digest = hashlib.sha256(cpf.encode()).digest()
        return digest[0] < self._corte


class BureauSempreLimpo:
    """Nunca reporta restricao. Util para isolar o efeito do score nos testes."""

    async def tem_restricao(self, cpf: str) -> bool:
        return False


class BureauSempreRestrito:
    """Sempre reporta restricao. Exercita o caminho de veto duro."""

    async def tem_restricao(self, cpf: str) -> bool:
        return True

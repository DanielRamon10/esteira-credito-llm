"""Ports do kyc-compliance.

Mesmo Dependency Inversion do outro servico, e por isso os mesmos `Protocol` em
vez de `ABC`: o adapter nao herda nada da camada de aplicacao.

Sao apenas dois ports, e a diferenca de tamanho em relacao ao `credit-analysis`
diz algo sobre o dominio: este servico nao chama LLM, nao faz OCR e nao consulta
bureau. Ele le uma lista e aplica regra. Um servico com poucas dependencias
externas e um servico facil de manter disponivel — o que importa aqui, porque ele
vira dependencia do outro.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import UUID

from kyc_compliance.domain.triagem import EntradaRestritiva, Triagem


@runtime_checkable
class RepositorioListas(Protocol):
    """Fonte das listas restritivas.

    Sincrono de proposito. O adapter atual le arquivo no boot e serve de memoria;
    marcar como `async` daria a falsa impressao de que a chamada libera o event
    loop. Quando a fonte virar uma API externa (COAF, OFAC), o port muda junto — e
    essa mudanca deve ser visivel, nao mascarada por uma assinatura otimista.
    """

    def todas(self) -> list[EntradaRestritiva]:
        """Todas as entradas carregadas."""
        ...

    @property
    def total(self) -> int: ...

    @property
    def procedencia(self) -> str:
        """De onde as listas vieram, para registro na trilha de auditoria."""
        ...


@runtime_checkable
class RepositorioTriagens(Protocol):
    """Persistencia do resultado.

    Guardar a triagem nao e cache: a Circular BCB 3.978 exige manter registro das
    diligencias por cinco anos. A consulta seria barata de repetir; a **prova de
    que foi feita naquela data, contra aquela versao da lista** e o que nao se
    reconstroi depois.
    """

    async def salvar(self, triagem: Triagem) -> None: ...

    async def buscar_por_id(self, triagem_id: UUID) -> Triagem | None: ...

    async def listar(self, limite: int = 50, offset: int = 0) -> list[Triagem]: ...

    async def contar(self) -> int: ...

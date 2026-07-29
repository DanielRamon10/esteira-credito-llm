"""Repositorio em memoria.

Adapter mais simples possivel do port `RepositorioAnalises`. Serve para os
testes e para rodar a API sem banco nenhum; na Camada 6 entra o adapter
Postgres implementando exatamente a mesma interface, e nada acima muda.

Nao e thread-safe por design — um processo Uvicorn com varios workers nao
compartilha este dicionario. Isso e aceitavel para dev e explicito aqui para
que ninguem o promova a producao por engano.
"""

from __future__ import annotations

from uuid import UUID

from credit_analysis.domain.entities import AnaliseCredito


class RepositorioAnalisesMemoria:
    """Implementacao in-process do port de persistencia."""

    def __init__(self) -> None:
        self._itens: dict[UUID, AnaliseCredito] = {}

    async def salvar(self, analise: AnaliseCredito) -> None:
        self._itens[analise.id] = analise

    async def buscar_por_id(self, analise_id: UUID) -> AnaliseCredito | None:
        return self._itens.get(analise_id)

    async def listar(self, limite: int = 50, offset: int = 0) -> list[AnaliseCredito]:
        # O indice de insercao entra como criterio de desempate: o relogio do
        # Windows tem resolucao de ~15ms, entao duas analises criadas em
        # sequencia recebem o mesmo `criada_em`. Ordenar so por timestamp
        # devolveria a ordem de insercao (sort estavel), que e o inverso do
        # esperado. O adapter Postgres tera o mesmo problema e resolve com
        # ORDER BY criada_em DESC, id DESC.
        indexadas = list(enumerate(self._itens.values()))
        ordenadas = sorted(indexadas, key=lambda par: (par[1].criada_em, par[0]), reverse=True)
        return [analise for _, analise in ordenadas[offset : offset + limite]]

    async def contar(self) -> int:
        return len(self._itens)

    def limpar(self) -> None:
        """Reset entre testes. Fora do port de proposito: nao e operacao de negocio."""
        self._itens.clear()

"""Fixtures compartilhadas.

A base de teste e definida aqui, e nao lida de `conhecimento/`: teste que depende de
arquivo de conteudo quebra quando alguem reescreve um artigo por outro motivo. O
carregamento do disco tem teste proprio, com arquivo temporario.

A excecao e o eval, que roda contra o corpus **real** de proposito — a medicao de
qualidade da busca sobre um corpus inventado nao diz nada sobre o corpus servido.
"""

from __future__ import annotations

import pytest

from customer_support.config import Ambiente, ProvedorLLM, Settings
from customer_support.domain.conhecimento import Artigo
from customer_support.domain.divulgacao import Visibilidade


class ConhecimentoFalso:
    """Base em memoria, com o mesmo contrato do adapter de arquivos."""

    def __init__(self, artigos: list[Artigo]) -> None:
        from plataforma.bm25 import IndiceBM25

        self._artigos = artigos
        self._publicos = [a for a in artigos if a.publico]
        self._indice_publico = IndiceBM25([a.texto_para_indexar for a in self._publicos])
        self._indice_completo = IndiceBM25([a.texto_para_indexar for a in artigos])

    def buscar(self, pergunta: str, k: int = 3, apenas_publicos: bool = True):  # type: ignore[no-untyped-def]
        from customer_support.domain.conhecimento import ArtigoRecuperado

        fonte = self._publicos if apenas_publicos else self._artigos
        indice = self._indice_publico if apenas_publicos else self._indice_completo
        if not pergunta.strip() or k <= 0:
            return []
        return [
            ArtigoRecuperado(artigo=fonte[i.indice], score=i.score)
            for i in indice.buscar(pergunta, k=k)
        ]

    def todos(self) -> list[Artigo]:
        return list(self._artigos)

    @property
    def total(self) -> int:
        return len(self._artigos)

    @property
    def publicos(self) -> int:
        return len(self._publicos)

    @property
    def procedencia(self) -> str:
        return "falso"


@pytest.fixture
def artigos() -> list[Artigo]:
    return [
        Artigo(
            id="comprovacao-renda",
            titulo="Como comprovar renda",
            texto=(
                "Assalariado envia os tres ultimos holerites. Autonomo envia o extrato "
                "bancario dos ultimos seis meses consecutivos."
            ),
        ),
        Artigo(
            id="portabilidade",
            titulo="Portabilidade de emprestimo",
            texto=(
                "Portabilidade e transferir seu emprestimo de outra instituicao para ca. "
                "Nao ha tarifa para o cliente."
            ),
        ),
        # Artigo INTERNO: existe para provar que a busca do cliente nao o alcanca.
        Artigo(
            id="limiares-internos",
            titulo="Limiares de score e alcadas",
            texto=(
                "Score acima de 700 pontos aprova direto. A alcada do gerente regional "
                "vai ate R$ 150.000. Conforme a POL-001, comprometimento acima de 50% "
                "e vedado."
            ),
            visibilidade=Visibilidade.INTERNA,
        ),
    ]


@pytest.fixture
def conhecimento(artigos: list[Artigo]) -> ConhecimentoFalso:
    return ConhecimentoFalso(artigos)


@pytest.fixture
def settings_teste() -> Settings:
    return Settings(
        ambiente=Ambiente.LOCAL,
        nivel_log="WARNING",
        log_json=False,
        # Explicito, e nao `auto`: com o Ollama instalado na maquina, o modo
        # automatico faria a suite chamar um modelo de verdade. Mesmo defeito que
        # apareceu no credit-analysis, ja resolvido aqui.
        provedor_llm=ProvedorLLM.ARTIGO,
    )

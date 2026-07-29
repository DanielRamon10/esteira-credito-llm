"""Retrieval hibrido: denso + lexical, fundidos por Reciprocal Rank Fusion.

Por que hibrido. As duas buscas erram em lugares diferentes:

- A busca **densa** entende parafrase ("quanto da renda pode comprometer" casa
  com "limites de comprometimento"), mas e ruim com identificador exato: para
  o modelo, "POL-003" e "POL-005" sao quase o mesmo vetor.
- A busca **lexical** (BM25) acerta identificador, numero de resolucao e termo
  raro, mas nao sabe que "teto" e "limite maximo" sao a mesma coisa.

Num corpus de politicas as duas situacoes aparecem o tempo todo — perguntas em
linguagem natural e referencias a codigos. Usar so uma delas perde metade dos
casos.

Por que RRF e nao soma ponderada de scores. Similaridade de cosseno vive em
[-1, 1] e score BM25 e ilimitado e depende do corpus. Somar os dois exige
normalizar, e toda normalizacao (min-max, z-score) e instavel: muda conforme o
conjunto de candidatos daquela consulta. RRF ignora a magnitude e usa so a
**posicao** no ranking:

    RRF(d) = sum_sobre_rankings( 1 / (k + posicao(d)) )

Sem parametro para calibrar por corpus, sem normalizacao fragil.

Medicao sobre o corpus de politicas (20 perguntas, acerto no nivel de secao —
ver `tests/eval/test_retrieval_qualidade.py`):

    estrategia            top-1    top-3
    lexical (BM25)        65,0%    95,0%
    denso (e5-large)      90,0%    95,0%
    hibrido (RRF)         85,0%   100,0%

O hibrido perde 5 pontos no top-1 e ganha os 5 que faltavam no top-3. Para RAG
o top-3 e a metrica que importa: o LLM recebe k trechos, e um trecho correto
em segundo lugar serve tanto quanto em primeiro. Zero falha no top-3 significa
que a fundamentacao sempre tem a politica certa disponivel.

Um caso concreto de por que o lexical sozinho nao basta: para "qual a
resolucao da LGPD...", o BM25 coloca em primeiro um trecho sobre "resolucao
minima de 200 DPI" — mesma palavra, sentido oposto. A busca densa nao cai
nessa.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from plataforma.bm25 import IndiceBM25

from credit_analysis.application.ports import Embedder, RepositorioPoliticas
from credit_analysis.domain.politica import TrechoPolitica, TrechoRecuperado
from credit_analysis.infrastructure.observabilidade import metricas
from credit_analysis.infrastructure.observabilidade.tracing import span

# Constante do RRF. Valor 60 e o do paper original (Cormack et al., 2009);
# amortece o peso das primeiras posicoes para que um unico ranking muito
# confiante nao domine a fusao.
#
# Varrido de 1 a 100 sobre o eval set do corpus (tests/eval): o resultado nao
# muda em nenhum ponto da faixa. Com apenas 2 rankings e poucas dezenas de
# candidatos, a fusao e dominada por "aparece nos dois" e nao pela posicao
# exata. Mantido no valor do paper por nao haver evidencia para mudar — se o
# corpus crescer uma ordem de grandeza, vale revarrer.
K_RRF = 60

# Quantos candidatos pedir a cada busca antes de fundir. Buscar mais que o k
# final da a RRF material para reordenar — se cada lado devolvesse so k, a
# fusao teria pouco o que cruzar.
FATOR_CANDIDATOS = 4


@dataclass(frozen=True, slots=True)
class ConfiguracaoBusca:
    """Parametros de uma consulta ao corpus."""

    k: int = 5
    produto: str | None = None
    usar_lexical: bool = True
    usar_denso: bool = True


class RetrieverHibrido:
    """Combina busca densa e lexical sobre o mesmo conjunto de trechos.

    O indice BM25 e construido sob demanda a partir do que esta no vector
    store e invalidado quando a contagem muda. Para um corpus de politicas —
    que muda por deploy, nao por requisicao — isso e suficiente e evita manter
    dois indices em sincronia.
    """

    def __init__(self, repositorio: RepositorioPoliticas, embedder: Embedder) -> None:
        self._repositorio = repositorio
        self._embedder = embedder
        self._indice: IndiceBM25 | None = None
        self._trechos: list[TrechoPolitica] = []
        self._tamanho_indexado = -1

    async def _garantir_indice_lexical(self) -> None:
        total = await self._repositorio.contar()
        if self._indice is not None and total == self._tamanho_indexado:
            return

        self._trechos = await self._repositorio.listar_todos()
        # Indexa o texto enriquecido (titulo + caminho da secao + corpo), o
        # mesmo que foi vetorizado. Se os dois lados vissem textos diferentes,
        # comparar posicoes na fusao nao faria sentido.
        self._indice = IndiceBM25([t.texto_para_indexar for t in self._trechos])
        self._tamanho_indexado = total

    async def buscar(
        self, consulta: str, config: ConfiguracaoBusca | None = None
    ) -> list[TrechoRecuperado]:
        """Recupera os trechos mais relevantes para a consulta.

        A medicao fica neste envelope e o algoritmo em `_executar_busca` porque a
        busca tem quatro saidas distintas (consulta vazia, nenhum ranking, um
        ranking, fusao). Instrumentar cada `return` daria quatro chances de
        esquecer uma na proxima alteracao — e uma metrica que perde caminhos
        mente para baixo, o que e pior que nao ter metrica.
        """
        cfg = config or ConfiguracaoBusca()
        if cfg.k <= 0 or not consulta.strip():
            return []

        inicio = time.perf_counter()
        with span(
            "rag.buscar",
            **{
                "rag.k": cfg.k,
                "rag.produto": cfg.produto or "todos",
                # A consulta em si NAO entra no span: e texto livre do usuario e
                # pode conter dado pessoal.
                "rag.tamanho_consulta": len(consulta),
            },
        ):
            resultados = await self._executar_busca(consulta, cfg)

        metricas.retrieval_duracao.observe(time.perf_counter() - inicio)
        metricas.retrieval_trechos.observe(len(resultados))
        return resultados

    async def _executar_busca(
        self, consulta: str, cfg: ConfiguracaoBusca
    ) -> list[TrechoRecuperado]:
        candidatos = cfg.k * FATOR_CANDIDATOS
        rankings: list[list[TrechoRecuperado]] = []

        if cfg.usar_denso:
            vetor = self._embedder.vetorizar_consulta(consulta)
            densos = await self._repositorio.buscar_denso(vetor, k=candidatos, produto=cfg.produto)
            if densos:
                rankings.append(densos)

        if cfg.usar_lexical:
            lexicais = await self._buscar_lexical(consulta, candidatos, cfg.produto)
            if lexicais:
                rankings.append(lexicais)

        if not rankings:
            return []
        if len(rankings) == 1:
            return rankings[0][: cfg.k]

        return _fundir_rrf(rankings)[: cfg.k]

    async def _buscar_lexical(
        self, consulta: str, k: int, produto: str | None
    ) -> list[TrechoRecuperado]:
        await self._garantir_indice_lexical()
        if self._indice is None:
            return []

        # Pede mais que k porque o filtro por produto e aplicado depois: sem
        # folga, filtrar poderia esvaziar o ranking lexical.
        brutos = self._indice.buscar(consulta, k=k * 2)

        resultados: list[TrechoRecuperado] = []
        for item in brutos:
            trecho = self._trechos[item.indice]
            if produto is not None and not trecho.aplicavel_a(produto):
                continue
            resultados.append(TrechoRecuperado(trecho=trecho, score=item.score, origem="lexical"))
            if len(resultados) == k:
                break

        return resultados


def _fundir_rrf(rankings: list[list[TrechoRecuperado]]) -> list[TrechoRecuperado]:
    """Reciprocal Rank Fusion sobre varios rankings."""
    pontuacao: dict[str, float] = {}
    melhor: dict[str, TrechoRecuperado] = {}
    origens: dict[str, set[str]] = {}

    for ranking in rankings:
        for posicao, item in enumerate(ranking, start=1):
            chave = item.trecho.id
            pontuacao[chave] = pontuacao.get(chave, 0.0) + 1.0 / (K_RRF + posicao)
            origens.setdefault(chave, set()).add(item.origem)
            # Guarda a instancia de melhor posicao para preservar o score
            # original do ranking em que o trecho foi mais bem colocado.
            if chave not in melhor:
                melhor[chave] = item

    ordenados = sorted(pontuacao.items(), key=lambda par: par[1], reverse=True)

    return [
        TrechoRecuperado(
            trecho=melhor[chave].trecho,
            score=score,
            origem="+".join(sorted(origens[chave])),
        )
        for chave, score in ordenados
    ]

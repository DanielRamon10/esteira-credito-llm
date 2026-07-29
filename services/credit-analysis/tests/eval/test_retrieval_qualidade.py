"""Avaliacao de qualidade do retrieval.

Isto nao e um teste unitario: e um **eval set** versionado. Testes unitarios
respondem "o codigo faz o que eu escrevi?"; este responde "o sistema recupera
a politica certa?". A segunda pergunta e a que importa num RAG, e e a que
regride silenciosamente quando alguem troca o modelo de embedding, mexe no
chunking ou ajusta a fusao.

Marcado como `eval` e desabilitado por padrao (ver `addopts` no pyproject):
baixa 2,24GB de modelo na primeira execucao. Rodar com:

    pytest -m eval

A metrica e acerto no nivel de **secao**, nao de documento. Acertar que a
resposta esta em POL-001 e facil — so ha seis politicas. O que sustenta uma
citacao e apontar a secao correta.
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterator

import pytest

from credit_analysis.domain.politica import TrechoRecuperado
from credit_analysis.infrastructure.rag.carregador import carregar_corpus
from credit_analysis.infrastructure.rag.embeddings import EmbedderFastEmbed
from credit_analysis.infrastructure.rag.retriever import ConfiguracaoBusca, RetrieverHibrido
from credit_analysis.infrastructure.rag.vector_store import VectorStoreMemoria

pytestmark = pytest.mark.eval

DIRETORIO_POLITICAS = pathlib.Path(__file__).parents[2] / "politicas"

# (pergunta, politica esperada, prefixo da secao esperada)
#
# As perguntas sao escritas como um analista falaria, nao copiando os termos do
# documento — se a pergunta repetisse o titulo da secao, o eval mediria
# casamento de string, nao retrieval.
EVAL_SET: list[tuple[str, str, str]] = [
    ("qual o teto de comprometimento de renda?", "POL-001", "2."),
    ("posso somar a renda do conjuge na proposta?", "POL-001", "3. Renda considerada / 3.1"),
    ("o que descontar da renda bruta antes do calculo?", "POL-001", "4."),
    ("quantos holerites preciso apresentar sendo CLT?", "POL-002", "2."),
    (
        "por quanto tempo vale um comprovante de residencia?",
        "POL-002",
        "3. Validade e qualidade dos documentos / 3.1",
    ),
    (
        "qual a confianca minima do OCR para nao ir para revisao humana?",
        "POL-002",
        "3. Validade e qualidade dos documentos / 3.2",
    ),
    ("protesto ativo impede a aprovacao?", "POL-003", "2."),
    ("o cliente pode pedir revisao de uma decisao automatica?", "POL-003", "5."),
    ("e se um bureau acusar restricao e outro nao?", "POL-003", "4."),
    ("qual a taxa de juros do consignado?", "POL-004", "3."),
    ("qual o score minimo para cartao de credito?", "POL-004", "4."),
    ("qual o prazo maximo do CDC?", "POL-004", "1."),
    ("quantos meses de extrato para autonomo?", "POL-005", "2."),
    ("uso media ou mediana para apurar renda variavel?", "POL-005", "3. C"),
    ("qual o redutor para renda instavel?", "POL-005", "4."),
    ("ate quanto o gerente de relacionamento pode aprovar?", "POL-006", "1."),
    ("quando a esteira deve mandar para analise manual?", "POL-006", "2."),
    ("por quanto tempo guardar o registro da decisao?", "POL-006", "5."),
    ("quais dados nao podem ser usados como fator de decisao?", "POL-006", "4."),
    (
        "quais creditos nao entram na apuracao de renda?",
        "POL-005",
        "3. Cálculo da renda apurada / 3.2",
    ),
]

# Piso de qualidade. Abaixo disso a suite falha e alguem precisa olhar o que
# mudou. Medido em 2026-07: hibrido entrega 85% top-1 e 100% top-3.
MINIMO_TOP1 = 0.80
MINIMO_TOP3 = 1.00


def _acertou(resultados: list[TrechoRecuperado], politica: str, secao: str, n: int) -> bool:
    return any(
        r.referencia.politica_id == politica and r.referencia.secao.startswith(secao)
        for r in resultados[:n]
    )


@pytest.fixture(scope="module")
def retriever() -> Iterator[RetrieverHibrido]:
    """Indexa o corpus uma vez para todo o modulo — o modelo e caro de carregar."""
    import asyncio

    trechos = carregar_corpus(DIRETORIO_POLITICAS)
    embedder = EmbedderFastEmbed()
    store = VectorStoreMemoria()

    vetores = embedder.vetorizar([t.texto_para_indexar for t in trechos])
    asyncio.run(store.indexar(trechos, vetores))

    yield RetrieverHibrido(store, embedder)


async def _medir(retriever: RetrieverHibrido, **flags: bool) -> tuple[float, float, list[str]]:
    top1 = top3 = 0
    falhas: list[str] = []

    for pergunta, politica, secao in EVAL_SET:
        resultados = await retriever.buscar(pergunta, ConfiguracaoBusca(k=3, **flags))
        top1 += _acertou(resultados, politica, secao, 1)
        if _acertou(resultados, politica, secao, 3):
            top3 += 1
        else:
            obtidos = ", ".join(str(r.referencia) for r in resultados)
            falhas.append(f"{pergunta!r} -> esperado {politica} {secao}*, veio: {obtidos}")

    total = len(EVAL_SET)
    return top1 / total, top3 / total, falhas


@pytest.mark.asyncio
async def test_hibrido_atinge_o_piso_de_qualidade(retriever: RetrieverHibrido) -> None:
    top1, top3, falhas = await _medir(retriever)

    assert top3 >= MINIMO_TOP3, (
        f"recall@3 caiu para {top3:.1%} (piso {MINIMO_TOP3:.0%}).\n" + "\n".join(falhas)
    )
    assert top1 >= MINIMO_TOP1, f"acerto@1 caiu para {top1:.1%} (piso {MINIMO_TOP1:.0%})"


@pytest.mark.asyncio
async def test_hibrido_supera_cada_estrategia_isolada_no_recall(
    retriever: RetrieverHibrido,
) -> None:
    """A justificativa do custo do hibrido: ele recupera o que nenhum dos dois sozinho recupera.

    Se este teste falhar, a complexidade da fusao deixou de se pagar e vale
    simplificar para a estrategia vencedora.
    """
    _, top3_hibrido, _ = await _medir(retriever)
    _, top3_lexical, _ = await _medir(retriever, usar_denso=False)
    _, top3_denso, _ = await _medir(retriever, usar_lexical=False)

    assert top3_hibrido >= top3_lexical
    assert top3_hibrido >= top3_denso
    assert top3_hibrido > min(top3_lexical, top3_denso)


@pytest.mark.asyncio
async def test_busca_por_codigo_de_politica_funciona(retriever: RetrieverHibrido) -> None:
    """Consulta por identificador exato — o caso em que a busca densa sozinha falha."""
    resultados = await retriever.buscar("POL-005 janela de apuracao", ConfiguracaoBusca(k=3))
    assert any(r.referencia.politica_id == "POL-005" for r in resultados)


@pytest.mark.asyncio
async def test_filtro_por_produto_restringe_o_resultado(retriever: RetrieverHibrido) -> None:
    """POL-005 nao se aplica a consignado; consulta filtrada nao pode traze-la."""
    resultados = await retriever.buscar(
        "renda variavel de autonomo", ConfiguracaoBusca(k=5, produto="consignado")
    )
    assert resultados
    assert all(r.trecho.aplicavel_a("consignado") for r in resultados)
    assert not any(r.referencia.politica_id == "POL-005" for r in resultados)

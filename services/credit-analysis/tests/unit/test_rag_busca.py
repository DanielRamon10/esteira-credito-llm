"""Testes de BM25, vector store e retrieval hibrido.

Usam `EmbedderFake` — deterministico e instantaneo. A qualidade semantica com
o modelo real e medida separadamente em `tests/eval`, que roda sob demanda.
Misturar as duas coisas tornaria a suite lenta sem aumentar a confianca no
que estes testes cobrem: o encanamento.
"""

from __future__ import annotations

from datetime import date

import pytest

from credit_analysis.domain.politica import ReferenciaPolitica, TrechoPolitica
from credit_analysis.infrastructure.rag.bm25 import IndiceBM25, tokenizar
from credit_analysis.infrastructure.rag.embeddings import EmbedderFake
from credit_analysis.infrastructure.rag.retriever import ConfiguracaoBusca, RetrieverHibrido
from credit_analysis.infrastructure.rag.vector_store import VectorStoreMemoria


def fazer_trecho(
    politica: str = "POL-001",
    secao: str = "1. Secao",
    texto: str = "texto de exemplo",
    produtos: frozenset[str] = frozenset(),
    versao: str = "1.0",
) -> TrechoPolitica:
    return TrechoPolitica(
        referencia=ReferenciaPolitica(politica_id=politica, versao=versao, secao=secao),
        titulo_politica="Politica de Teste",
        caminho_secao=(secao,),
        texto=texto,
        produtos=produtos,
        vigencia_inicio=date(2025, 1, 1),
    )


class TestTokenizacao:
    def test_remove_acento(self) -> None:
        # O corpus escreve "vigência" e o usuario digita "vigencia".
        assert tokenizar("vigência") == tokenizar("vigencia")

    def test_preserva_codigo_com_hifen(self) -> None:
        # "POL-001" e o termo mais discriminativo do corpus; parti-lo perde o sinal.
        assert "pol-001" in tokenizar("conforme a POL-001 secao 2")

    def test_preserva_numeros(self) -> None:
        assert "4966" in tokenizar("Resolucao 4966")
        assert "50" in tokenizar("teto de 50%")

    def test_remove_stopwords(self) -> None:
        assert tokenizar("o de a para com") == []


class TestBM25:
    def test_ranqueia_documento_com_termo_raro_acima(self) -> None:
        docs = [
            "credito consignado com desconto em folha",
            "credito pessoal sem garantia",
            "cartao de credito rotativo",
        ]
        indice = IndiceBM25(docs)
        assert indice.buscar("consignado", k=1)[0].indice == 0

    def test_termo_ausente_nao_pontua(self) -> None:
        indice = IndiceBM25(["credito consignado", "credito pessoal"])
        assert indice.buscar("hipoteca", k=5) == []

    def test_consulta_vazia_devolve_vazio(self) -> None:
        indice = IndiceBM25(["algum texto aqui"])
        assert indice.buscar("", k=5) == []
        assert indice.buscar("de a o", k=5) == []  # so stopwords

    def test_indice_vazio_nao_quebra(self) -> None:
        assert IndiceBM25([]).buscar("qualquer", k=5) == []

    def test_saturacao_de_frequencia(self) -> None:
        """A decima ocorrencia vale muito menos que a segunda — e isso que separa
        BM25 de TF-IDF e evita que spam de palavra domine o ranking."""
        indice = IndiceBM25(["risco " * 2 + "outro", "risco " * 40 + "outro"])
        scores = {r.indice: r.score for r in indice.buscar("risco", k=2)}
        # Vinte vezes mais ocorrencias nao pode dar vinte vezes mais pontos.
        assert scores[1] < scores[0] * 3

    def test_documento_curto_e_favorecido_com_mesma_frequencia(self) -> None:
        indice = IndiceBM25(["consignado", "consignado " + "enchimento " * 100])
        assert indice.buscar("consignado", k=1)[0].indice == 0


class TestVectorStoreMemoria:
    async def test_indexa_e_recupera(self) -> None:
        store = VectorStoreMemoria()
        embedder = EmbedderFake()
        trechos = [
            fazer_trecho(secao="1.", texto="comprometimento de renda e teto"),
            fazer_trecho(secao="2.", texto="restricao cadastral impeditiva"),
        ]
        await store.indexar(trechos, embedder.vetorizar([t.texto for t in trechos]))

        assert await store.contar() == 2
        resultados = await store.buscar_denso(
            embedder.vetorizar_consulta("comprometimento de renda e teto"), k=1
        )
        assert resultados[0].referencia.secao == "1."
        assert resultados[0].origem == "denso"

    async def test_reindexar_atualiza_em_vez_de_duplicar(self) -> None:
        # Rodar a ingestao duas vezes nao pode inflar o indice.
        store = VectorStoreMemoria()
        embedder = EmbedderFake()
        trecho = fazer_trecho(texto="original")

        await store.indexar([trecho], embedder.vetorizar(["original"]))
        await store.indexar([trecho], embedder.vetorizar(["original"]))

        assert await store.contar() == 1

    async def test_dimensao_incompativel_falha_explicitamente(self) -> None:
        # Trocar de modelo sem reindexar e um erro silencioso classico.
        store = VectorStoreMemoria()
        await store.indexar([fazer_trecho()], [[0.1] * 8])

        with pytest.raises(ValueError, match="incompativel"):
            await store.indexar([fazer_trecho(secao="2.")], [[0.1] * 16])

    async def test_quantidades_divergentes_falham(self) -> None:
        store = VectorStoreMemoria()
        with pytest.raises(ValueError, match="difere"):
            await store.indexar([fazer_trecho()], [[0.1] * 4, [0.2] * 4])

    async def test_filtro_por_produto(self) -> None:
        store = VectorStoreMemoria()
        embedder = EmbedderFake()
        trechos = [
            fazer_trecho(secao="1.", texto="regra de cdc", produtos=frozenset({"cdc"})),
            fazer_trecho(
                secao="2.", texto="regra de consignado", produtos=frozenset({"consignado"})
            ),
        ]
        await store.indexar(trechos, embedder.vetorizar([t.texto for t in trechos]))

        resultados = await store.buscar_denso(
            embedder.vetorizar_consulta("regra"), k=5, produto="cdc"
        )
        assert len(resultados) == 1
        assert resultados[0].referencia.secao == "1."

    async def test_trecho_sem_produto_vale_para_todos(self) -> None:
        store = VectorStoreMemoria()
        embedder = EmbedderFake()
        trecho = fazer_trecho(texto="regra geral", produtos=frozenset())
        await store.indexar([trecho], embedder.vetorizar(["regra geral"]))

        assert await store.buscar_denso(
            embedder.vetorizar_consulta("regra geral"), k=5, produto="cartao"
        )

    async def test_store_vazio_devolve_vazio(self) -> None:
        store = VectorStoreMemoria()
        assert await store.buscar_denso([0.1] * 4, k=5) == []


class TestRetrieverHibrido:
    async def _montar(self, trechos: list[TrechoPolitica]) -> RetrieverHibrido:
        store = VectorStoreMemoria()
        embedder = EmbedderFake()
        await store.indexar(trechos, embedder.vetorizar([t.texto_para_indexar for t in trechos]))
        return RetrieverHibrido(store, embedder)

    async def test_fusao_marca_as_duas_origens(self) -> None:
        trechos = [
            fazer_trecho(secao="1.", texto="comprometimento de renda acima de cinquenta"),
            fazer_trecho(secao="2.", texto="documentacao obrigatoria por modalidade"),
        ]
        retriever = await self._montar(trechos)
        resultados = await retriever.buscar("comprometimento de renda", ConfiguracaoBusca(k=2))

        assert resultados
        assert "+" in resultados[0].origem  # veio de denso e lexical

    async def test_estrategia_isolada_preserva_a_origem(self) -> None:
        retriever = await self._montar([fazer_trecho(texto="consignado desconto em folha")])

        so_lexical = await retriever.buscar("consignado", ConfiguracaoBusca(k=1, usar_denso=False))
        assert so_lexical[0].origem == "lexical"

        so_denso = await retriever.buscar(
            "consignado desconto em folha", ConfiguracaoBusca(k=1, usar_lexical=False)
        )
        assert so_denso[0].origem == "denso"

    async def test_consulta_vazia_devolve_vazio(self) -> None:
        retriever = await self._montar([fazer_trecho()])
        assert await retriever.buscar("   ", ConfiguracaoBusca(k=3)) == []

    async def test_k_zero_devolve_vazio(self) -> None:
        retriever = await self._montar([fazer_trecho()])
        assert await retriever.buscar("qualquer", ConfiguracaoBusca(k=0)) == []

    async def test_respeita_o_limite_k(self) -> None:
        trechos = [fazer_trecho(secao=f"{i}.", texto=f"regra {i} de credito") for i in range(10)]
        retriever = await self._montar(trechos)
        assert len(await retriever.buscar("credito", ConfiguracaoBusca(k=3))) == 3

    async def test_indice_lexical_reflete_reindexacao(self) -> None:
        """O indice BM25 e derivado do vector store e precisa invalidar quando ele cresce.

        Isolado em `usar_denso=False` de proposito: a busca densa sempre devolve
        os k mais proximos, mesmo sem nenhum trecho relevante, entao ela nao
        distingue "indice desatualizado" de "nada a ver". O lexical distingue.
        """
        so_lexical = ConfiguracaoBusca(k=3, usar_denso=False)
        store = VectorStoreMemoria()
        embedder = EmbedderFake()
        retriever = RetrieverHibrido(store, embedder)

        primeiro = fazer_trecho(secao="1.", texto="regra sobre consignado")
        await store.indexar([primeiro], embedder.vetorizar([primeiro.texto_para_indexar]))
        assert await retriever.buscar("hipoteca", so_lexical) == []

        segundo = fazer_trecho(secao="2.", texto="regra sobre hipoteca residencial")
        await store.indexar([segundo], embedder.vetorizar([segundo.texto_para_indexar]))
        resultados = await retriever.buscar("hipoteca", so_lexical)

        assert [r.referencia.secao for r in resultados] == ["2."]

    async def test_busca_densa_sempre_devolve_k_mesmo_sem_relevancia(self) -> None:
        """Limitacao conhecida, documentada de proposito.

        Nao ha piso de similaridade: uma pergunta fora do dominio recebe os k
        trechos menos ruins em vez de nenhum. Calibrar esse piso depende do
        modelo (as similaridades do e5 sao comprimidas numa faixa estreita) e
        seria chute sem medicao. O que protege o parecer nao e o piso, e a
        verificacao de citacao: o LLM so pode citar o que foi recuperado, e
        citacao nao confirmada e rejeitada.
        """
        retriever = await self._montar([fazer_trecho(texto="regra sobre consignado")])
        resultados = await retriever.buscar(
            "receita de bolo de cenoura", ConfiguracaoBusca(k=3, usar_lexical=False)
        )
        assert len(resultados) == 1

    async def test_filtro_por_produto_vale_para_os_dois_lados(self) -> None:
        trechos = [
            fazer_trecho(secao="1.", texto="prazo do cdc", produtos=frozenset({"cdc"})),
            fazer_trecho(
                secao="2.", texto="prazo do consignado", produtos=frozenset({"consignado"})
            ),
        ]
        retriever = await self._montar(trechos)
        resultados = await retriever.buscar("prazo", ConfiguracaoBusca(k=5, produto="consignado"))
        assert all(r.trecho.aplicavel_a("consignado") for r in resultados)

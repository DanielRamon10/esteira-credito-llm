"""Testes de integracao do adapter pgvector.

Exercitam o Postgres de verdade. Sao pulados quando o banco nao esta de pe,
para que `pytest` continue funcionando numa maquina sem Docker — mas nao sao
silenciosos: o motivo do skip aparece com `-rs`.

    docker compose up -d
    cp .env.example .env      # ou exporte CREDIT_POSTGRES_DSN
    pytest -m integration

O banco usado e sempre `<nome>_test`, derivado do DSN — nunca o de
desenvolvimento, porque estes testes truncam a tabela.

Usam `EmbedderFake` com vetores de 1024 dimensoes, casando com o schema. O que
se testa aqui e o adapter — SQL, upsert, filtro, ordenacao — nao a qualidade
semantica, que e medida em `tests/eval`.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import date
from urllib.parse import urlsplit, urlunsplit

import pytest

from credit_analysis.config import get_settings
from credit_analysis.domain.politica import ReferenciaPolitica, TrechoPolitica
from credit_analysis.infrastructure.rag.embeddings import EmbedderFake
from credit_analysis.infrastructure.rag.pgvector_store import (
    DIMENSAO_ESPERADA,
    VectorStorePgVector,
    criar_pool,
)
from credit_analysis.infrastructure.rag.retriever import ConfiguracaoBusca, RetrieverHibrido

SUFIXO_TESTE = "_test"


def _dsn_de_teste() -> str:
    """Deriva o DSN de teste, nunca reutilizando o banco de desenvolvimento.

    Estes testes dao TRUNCATE na tabela de trechos. Apontados para o banco de
    desenvolvimento, apagam o corpus ingerido — e o sintoma ("a busca nao
    retorna nada") aparece muito depois da causa. Pior: contra um banco
    compartilhado, apagariam dado de outra pessoa.

    Por isso o nome do banco e trocado por `<nome>_test` e ha uma verificacao
    explicita depois: se o DSN nao terminar no sufixo, os testes nao rodam.

    A origem do DSN passa pelo `Settings` e nao so por `os.getenv`: o README
    manda copiar o `.env.example` para `.env`, e lendo apenas o ambiente estes
    testes pulariam em silencio numa maquina com o banco de pe e o `.env` no
    lugar. Suite verde escondendo quinze testes de integracao e pior que suite
    vermelha.
    """
    bruto = os.getenv("CREDIT_POSTGRES_DSN_TEST") or get_settings().postgres_dsn.strip()
    if not bruto:
        return ""

    partes = urlsplit(bruto)
    banco = partes.path.lstrip("/")
    if not banco.endswith(SUFIXO_TESTE):
        banco += SUFIXO_TESTE

    return urlunsplit(partes._replace(path=f"/{banco}"))


DSN = _dsn_de_teste()

# Rede de seguranca: mesmo com o DSN sobrescrito na mao, nao tocamos num banco
# que nao se identifique como de teste.
_BANCO_E_DE_TESTE = bool(DSN) and urlsplit(DSN).path.lstrip("/").endswith(SUFIXO_TESTE)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not DSN,
        reason="CREDIT_POSTGRES_DSN nao definido; suba o banco com `docker compose up -d`",
    ),
    pytest.mark.skipif(
        bool(DSN) and not _BANCO_E_DE_TESTE,
        reason=f"recusando rodar: o banco do DSN nao termina em '{SUFIXO_TESTE}'",
    ),
]


@pytest.fixture
async def store() -> AsyncIterator[VectorStorePgVector]:
    """Store ligado ao Postgres, com a tabela limpa antes e depois."""
    pool = criar_pool(DSN, minimo=1, maximo=2)
    await pool.open(wait=True, timeout=10)
    adapter = VectorStorePgVector(pool)
    await adapter.limpar()
    try:
        yield adapter
    finally:
        await adapter.limpar()
        await pool.close()


@pytest.fixture
def embedder() -> EmbedderFake:
    # Precisa casar com a coluna vector(1024) do schema.
    return EmbedderFake(dimensoes=DIMENSAO_ESPERADA)


def fazer_trecho(
    politica: str = "POL-001",
    secao: str = "1. Secao",
    texto: str = "texto de politica para teste",
    produtos: frozenset[str] = frozenset(),
    versao: str = "1.0",
) -> TrechoPolitica:
    return TrechoPolitica(
        referencia=ReferenciaPolitica(politica_id=politica, versao=versao, secao=secao),
        titulo_politica=f"Politica {politica}",
        caminho_secao=tuple(secao.split(" / ")),
        texto=texto,
        produtos=produtos,
        vigencia_inicio=date(2025, 1, 1),
        area="Risco",
    )


class TestPersistencia:
    async def test_indexa_e_conta(self, store: VectorStorePgVector, embedder: EmbedderFake) -> None:
        trechos = [fazer_trecho(secao=f"{i}.") for i in range(3)]
        await store.indexar(trechos, embedder.vetorizar([t.texto for t in trechos]))

        assert await store.contar() == 3

    async def test_upsert_nao_duplica(
        self, store: VectorStorePgVector, embedder: EmbedderFake
    ) -> None:
        # Reingestao e operacao normal de deploy; nao pode inflar o indice.
        trecho = fazer_trecho()
        vetor = embedder.vetorizar([trecho.texto])

        await store.indexar([trecho], vetor)
        await store.indexar([trecho], vetor)

        assert await store.contar() == 1

    async def test_upsert_atualiza_o_texto(
        self, store: VectorStorePgVector, embedder: EmbedderFake
    ) -> None:
        original = fazer_trecho(texto="versao antiga do texto da politica")
        await store.indexar([original], embedder.vetorizar([original.texto]))

        # Mesma referencia (mesmo id), conteudo novo.
        revisado = fazer_trecho(texto="versao revisada do texto da politica")
        await store.indexar([revisado], embedder.vetorizar([revisado.texto]))

        armazenados = await store.listar_todos()
        assert len(armazenados) == 1
        assert "revisada" in armazenados[0].texto

    async def test_preserva_todos_os_metadados(
        self, store: VectorStorePgVector, embedder: EmbedderFake
    ) -> None:
        trecho = fazer_trecho(
            politica="POL-007",
            secao="3. Pai / 3.1 Filha",
            produtos=frozenset({"cdc", "consignado"}),
            versao="2.5",
        )
        await store.indexar([trecho], embedder.vetorizar([trecho.texto]))

        recuperado = (await store.listar_todos())[0]
        assert recuperado.referencia == trecho.referencia
        assert recuperado.produtos == trecho.produtos
        assert recuperado.caminho_secao == ("3. Pai", "3.1 Filha")
        assert recuperado.vigencia_inicio == date(2025, 1, 1)
        assert recuperado.area == "Risco"

    async def test_limpar_esvazia(self, store: VectorStorePgVector, embedder: EmbedderFake) -> None:
        await store.indexar([fazer_trecho()], embedder.vetorizar(["x"]))
        await store.limpar()
        assert await store.contar() == 0


class TestBusca:
    async def test_recupera_o_mais_similar(
        self, store: VectorStorePgVector, embedder: EmbedderFake
    ) -> None:
        trechos = [
            fazer_trecho(secao="1.", texto="comprometimento de renda e teto de cinquenta"),
            fazer_trecho(secao="2.", texto="prazo maximo do credito consignado noventa e seis"),
        ]
        await store.indexar(trechos, embedder.vetorizar([t.texto for t in trechos]))

        resultados = await store.buscar_denso(
            embedder.vetorizar_consulta("comprometimento de renda e teto de cinquenta"), k=1
        )
        assert resultados[0].referencia.secao == "1."
        assert resultados[0].origem == "denso"

    async def test_score_e_similaridade_e_nao_distancia(
        self, store: VectorStorePgVector, embedder: EmbedderFake
    ) -> None:
        # O SQL usa `<=>` (distancia) e converte com 1 - d. Se a conversao
        # sumir, o ranking inverte silenciosamente.
        texto = "regra de politica sobre restricao cadastral"
        await store.indexar([fazer_trecho(texto=texto)], embedder.vetorizar([texto]))

        resultado = (await store.buscar_denso(embedder.vetorizar_consulta(texto), k=1))[0]
        assert resultado.score > 0.99

    async def test_ordena_do_mais_similar_para_o_menos(
        self, store: VectorStorePgVector, embedder: EmbedderFake
    ) -> None:
        trechos = [fazer_trecho(secao=f"{i}.", texto=f"assunto numero {i}") for i in range(5)]
        await store.indexar(trechos, embedder.vetorizar([t.texto for t in trechos]))

        resultados = await store.buscar_denso(embedder.vetorizar_consulta("assunto numero 3"), k=5)
        scores = [r.score for r in resultados]
        assert scores == sorted(scores, reverse=True)

    async def test_respeita_o_limite(
        self, store: VectorStorePgVector, embedder: EmbedderFake
    ) -> None:
        trechos = [fazer_trecho(secao=f"{i}.") for i in range(10)]
        await store.indexar(trechos, embedder.vetorizar([t.texto for t in trechos]))

        assert len(await store.buscar_denso(embedder.vetorizar_consulta("x"), k=4)) == 4

    async def test_filtro_por_produto(
        self, store: VectorStorePgVector, embedder: EmbedderFake
    ) -> None:
        trechos = [
            fazer_trecho(secao="1.", texto="regra a", produtos=frozenset({"cdc"})),
            fazer_trecho(secao="2.", texto="regra b", produtos=frozenset({"consignado"})),
            fazer_trecho(secao="3.", texto="regra c", produtos=frozenset()),
        ]
        await store.indexar(trechos, embedder.vetorizar([t.texto for t in trechos]))

        resultados = await store.buscar_denso(
            embedder.vetorizar_consulta("regra"), k=10, produto="cdc"
        )
        secoes = {r.referencia.secao for r in resultados}
        # Trecho sem produto declarado vale para todos — mesma semantica do
        # adapter em memoria.
        assert secoes == {"1.", "3."}

    async def test_store_vazio_devolve_vazio(
        self, store: VectorStorePgVector, embedder: EmbedderFake
    ) -> None:
        assert await store.buscar_denso(embedder.vetorizar_consulta("x"), k=5) == []


class TestValidacao:
    async def test_dimensao_errada_falha_antes_do_insert(self, store: VectorStorePgVector) -> None:
        # Erro claro no adapter em vez de erro de tipo do Postgres no meio do lote.
        with pytest.raises(ValueError, match="dimensoes"):
            await store.indexar([fazer_trecho()], [[0.1] * 64])

    async def test_quantidades_divergentes_falham(self, store: VectorStorePgVector) -> None:
        with pytest.raises(ValueError, match="difere"):
            await store.indexar([fazer_trecho()], [[0.1] * DIMENSAO_ESPERADA] * 2)

    async def test_lote_vazio_e_no_op(self, store: VectorStorePgVector) -> None:
        await store.indexar([], [])
        assert await store.contar() == 0


class TestRetrieverSobrePgVector:
    async def test_hibrido_funciona_com_o_adapter_persistido(
        self, store: VectorStorePgVector, embedder: EmbedderFake
    ) -> None:
        """O retriever nao sabe qual adapter esta por baixo — este teste prova."""
        trechos = [
            fazer_trecho(secao="1.", texto="o teto de comprometimento de renda e cinquenta"),
            fazer_trecho(secao="2.", texto="documentacao obrigatoria por modalidade de credito"),
        ]
        await store.indexar(trechos, embedder.vetorizar([t.texto_para_indexar for t in trechos]))

        retriever = RetrieverHibrido(store, embedder)
        resultados = await retriever.buscar(
            "teto de comprometimento de renda", ConfiguracaoBusca(k=2)
        )

        assert resultados
        assert resultados[0].referencia.secao == "1."
        assert "+" in resultados[0].origem  # fundiu denso e lexical

"""Testes de integracao das rotas de politicas."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from credit_analysis.api.app import criar_app
from credit_analysis.config import Settings
from credit_analysis.domain.politica import ReferenciaPolitica, TrechoPolitica
from credit_analysis.infrastructure.llm.anthropic_adapter import LLMFake
from credit_analysis.infrastructure.rag.embeddings import EmbedderFake
from credit_analysis.infrastructure.rag.retriever import RetrieverHibrido
from credit_analysis.infrastructure.rag.vector_store import VectorStoreMemoria
from tests.conftest import emitir_token, montar_cliente

pytestmark = pytest.mark.integration

TEXTO_TETO = "O comprometimento acima de 50% e vedado. O teto de 50% e limite duro de politica."
TEXTO_CONSIGNADO = "O credito consignado tem prazo de 12 a 96 meses e taxa de 1,20% a 1,85% ao mes."


def fazer_trecho(
    politica: str, secao: str, texto: str, produtos: frozenset[str] = frozenset()
) -> TrechoPolitica:
    return TrechoPolitica(
        referencia=ReferenciaPolitica(politica_id=politica, versao="1.0", secao=secao),
        titulo_politica=f"Politica {politica}",
        caminho_secao=(secao,),
        texto=texto,
        produtos=produtos,
        vigencia_inicio=date(2025, 1, 1),
    )


TRECHOS = [
    fazer_trecho("POL-001", "2. Faixas", TEXTO_TETO),
    fazer_trecho("POL-004", "3. Consignado", TEXTO_CONSIGNADO, frozenset({"consignado"})),
]


@pytest.fixture
def client_rag(settings_teste: Settings, chaves_de_teste: Path) -> Iterator[TestClient]:
    """API com o corpus em memoria e LLM fake — sem Postgres, sem chave."""
    import asyncio

    store = VectorStoreMemoria()
    embedder = EmbedderFake()
    asyncio.run(store.indexar(TRECHOS, embedder.vetorizar([t.texto_para_indexar for t in TRECHOS])))

    app = criar_app(
        settings=settings_teste,
        retriever=RetrieverHibrido(store, embedder),
        llm=LLMFake(),
    )
    with montar_cliente(app, emitir_token(chaves_de_teste)) as c:
        yield c


@pytest.fixture
def client_sem_rag(settings_teste: Settings, chaves_de_teste: Path) -> Iterator[TestClient]:
    """API sem indice configurado — exercita a degradacao."""
    with montar_cliente(
        criar_app(settings=settings_teste, llm=LLMFake()), emitir_token(chaves_de_teste)
    ) as c:
        yield c


class TestBusca:
    def test_retorna_trechos_com_procedencia(self, client_rag: TestClient) -> None:
        resposta = client_rag.get("/v1/politicas/buscar", params={"q": "teto de 50%"})
        assert resposta.status_code == 200

        corpo = resposta.json()
        assert corpo
        assert corpo[0]["politica_id"] == "POL-001"
        assert corpo[0]["origem"]  # denso, lexical ou fusao
        assert corpo[0]["versao"] == "1.0"

    def test_respeita_o_limite(self, client_rag: TestClient) -> None:
        corpo = client_rag.get("/v1/politicas/buscar", params={"q": "credito", "k": 1}).json()
        assert len(corpo) <= 1

    def test_filtra_por_produto(self, client_rag: TestClient) -> None:
        corpo = client_rag.get(
            "/v1/politicas/buscar", params={"q": "prazo e taxa", "produto": "cdc"}
        ).json()
        # POL-004 secao 3 e exclusiva de consignado.
        assert all(item["politica_id"] != "POL-004" for item in corpo)

    def test_consulta_curta_e_rejeitada(self, client_rag: TestClient) -> None:
        assert client_rag.get("/v1/politicas/buscar", params={"q": "ab"}).status_code == 422


class TestConsultaFundamentada:
    def test_devolve_citacoes_verificadas(self, client_rag: TestClient) -> None:
        resposta = client_rag.post(
            "/v1/politicas/consultar",
            json={"pergunta": "Qual o teto de comprometimento de renda?"},
        )
        assert resposta.status_code == 200

        corpo = resposta.json()
        assert corpo["texto"]
        assert corpo["citacoes"]
        assert corpo["politicas_consultadas"]
        # O LLM fake cita literalmente, entao nada deve ser rejeitado.
        assert corpo["citacoes_rejeitadas"] == []
        assert corpo["confiavel"] is True

    def test_citacao_aponta_politica_versao_e_secao(self, client_rag: TestClient) -> None:
        corpo = client_rag.post(
            "/v1/politicas/consultar", json={"pergunta": "Qual o teto de comprometimento?"}
        ).json()
        citacao = corpo["citacoes"][0]

        assert citacao["politica_id"].startswith("POL-")
        assert citacao["versao"]
        assert citacao["secao"]
        assert citacao["trecho_citado"]

    def test_expoe_citacao_rejeitada_em_vez_de_esconder(
        self, settings_teste: Settings, chaves_de_teste: Path
    ) -> None:
        """Um modelo que inventa citacao deve produzir resposta nao confiavel."""
        import asyncio
        import json

        store = VectorStoreMemoria()
        embedder = EmbedderFake()
        asyncio.run(
            store.indexar(TRECHOS, embedder.vetorizar([t.texto_para_indexar for t in TRECHOS]))
        )

        alucinado = json.dumps(
            {
                "fundamentacao": "Resposta com citacao fabricada.",
                "citacoes": [
                    {
                        "politica": "POL-001",
                        "versao": "1.0",
                        "secao": "2. Faixas",
                        "trecho": "O teto de comprometimento e de 90% para clientes VIP.",
                    }
                ],
            },
            ensure_ascii=False,
        )

        app = criar_app(
            settings=settings_teste,
            retriever=RetrieverHibrido(store, embedder),
            llm=LLMFake(alucinado),
        )
        with montar_cliente(app, emitir_token(chaves_de_teste)) as client:
            corpo = client.post("/v1/politicas/consultar", json={"pergunta": "Qual o teto?"}).json()

        assert corpo["confiavel"] is False
        assert corpo["citacoes"] == []
        assert corpo["citacoes_rejeitadas"]

    def test_pergunta_curta_e_rejeitada(self, client_rag: TestClient) -> None:
        resposta = client_rag.post("/v1/politicas/consultar", json={"pergunta": "oi"})
        assert resposta.status_code == 422


class TestDegradacaoSemIndice:
    def test_busca_responde_503_com_instrucao(self, client_sem_rag: TestClient) -> None:
        resposta = client_sem_rag.get("/v1/politicas/buscar", params={"q": "qualquer coisa"})

        assert resposta.status_code == 503
        corpo = resposta.json()
        assert corpo["codigo"] == "recurso_indisponivel"
        # A mensagem precisa dizer o que fazer, nao so que falhou.
        assert "docker compose" in corpo["mensagem"]
        assert "ingestao" in corpo["mensagem"]

    def test_consulta_responde_503(self, client_sem_rag: TestClient) -> None:
        resposta = client_sem_rag.post(
            "/v1/politicas/consultar", json={"pergunta": "Qual o teto de comprometimento?"}
        )
        assert resposta.status_code == 503

    def test_esteira_de_credito_continua_funcionando(
        self, client_sem_rag: TestClient, payload_analise: dict[str, object]
    ) -> None:
        """RAG indisponivel nao pode derrubar o resto do servico."""
        assert client_sem_rag.post("/v1/analises", json=payload_analise).status_code == 201
        assert client_sem_rag.get("/health").status_code == 200

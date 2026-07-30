"""Testes de integracao da rota do agente.

Dois desfechos importam aqui, e o segundo mais que o primeiro: com agente
configurado, o contrato precisa expor a trilha; **sem** agente, precisa
responder 503 com instrucao em vez de inventar um atendimento.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from credit_analysis.api.app import criar_app
from credit_analysis.config import Settings
from credit_analysis.infrastructure.agente.grafo import AgenteLangGraph
from credit_analysis.infrastructure.llm.anthropic_adapter import LLMFake
from tests.apoio.chat_falso import ChatFalso, decisao_com_ferramenta, resposta_final
from tests.conftest import emitir_token, montar_cliente

pytestmark = pytest.mark.integration


@pytest.fixture
def client_com_agente(settings_teste: Settings, chaves_de_teste: Path) -> Iterator[TestClient]:
    modelo = ChatFalso(
        respostas=[
            decisao_com_ferramenta(
                "simular_proposta", valor=30000, prazo_meses=48, renda_mensal=8000
            ),
            resposta_final("A parcela cabe na renda: comprometimento de 12,2%."),
        ]
    )
    app = criar_app(
        settings=settings_teste,
        llm=LLMFake(),
        agente=AgenteLangGraph(modelo=modelo, identificacao="teste:falso"),
    )
    with montar_cliente(app, emitir_token(chaves_de_teste)) as c:
        yield c


@pytest.fixture
def client_sem_agente(settings_teste: Settings, chaves_de_teste: Path) -> Iterator[TestClient]:
    """`settings_teste` fixa provedor_llm=fake, e nesse modo o agente nao sobe.

    Deliberado: um agente falso responderia com texto plausivel e trilha vazia,
    e quem consome a API nao teria como saber que nenhuma ferramenta rodou.
    """
    with montar_cliente(
        criar_app(settings=settings_teste, llm=LLMFake()), emitir_token(chaves_de_teste)
    ) as c:
        yield c


class TestConsulta:
    def test_devolve_resposta_e_trilha(self, client_com_agente: TestClient) -> None:
        resposta = client_com_agente.post(
            "/v1/agente/consultar",
            json={"pergunta": "Uma proposta de 30 mil em 48 meses cabe numa renda de 8 mil?"},
        )
        assert resposta.status_code == 200

        corpo = resposta.json()
        assert "12,2" in corpo["resposta"]
        assert corpo["completa"] is True
        assert corpo["motivo_parada"] == "respondeu"
        assert corpo["modelo"] == "teste:falso"
        assert corpo["ferramentas_usadas"] == ["simular_proposta"]
        assert corpo["injecao_suspeita"] is False

    def test_trilha_expoe_argumentos_validados(self, client_com_agente: TestClient) -> None:
        # O contrato mostra os argumentos que a ferramenta recebeu, nao o texto
        # cru do modelo — e o que permite conferir a conta depois.
        resposta = client_com_agente.post(
            "/v1/agente/consultar", json={"pergunta": "simule 30 mil em 48 meses"}
        )

        passo = resposta.json()["passos"][0]
        assert passo["ordem"] == 1
        assert passo["ferramenta"] == "simular_proposta"
        assert passo["argumentos"]["prazo_meses"] == 48
        assert passo["sucesso"] is True
        assert "score" in passo["resumo"]

    def test_pergunta_vazia_e_422(self, client_com_agente: TestClient) -> None:
        resposta = client_com_agente.post("/v1/agente/consultar", json={"pergunta": ""})
        assert resposta.status_code == 422

    def test_analise_id_invalido_e_422(self, client_com_agente: TestClient) -> None:
        # A validacao na borda importa aqui mais que no resto da API: este campo
        # e o que fixa qual caso o agente pode ler.
        resposta = client_com_agente.post(
            "/v1/agente/consultar",
            json={"pergunta": "resuma o caso", "analise_id": "nao-e-uuid"},
        )
        assert resposta.status_code == 422

    def test_analise_id_e_aceito_no_corpo(self, client_com_agente: TestClient) -> None:
        resposta = client_com_agente.post(
            "/v1/agente/consultar",
            json={"pergunta": "simule 30 mil em 48 meses", "analise_id": str(uuid4())},
        )
        assert resposta.status_code == 200


class TestDegradacao:
    def test_sem_agente_responde_503_com_instrucao(self, client_sem_agente: TestClient) -> None:
        resposta = client_sem_agente.post(
            "/v1/agente/consultar", json={"pergunta": "Qual o teto de comprometimento?"}
        )
        assert resposta.status_code == 503

        corpo = resposta.json()
        assert "ollama" in corpo["mensagem"].lower(), "a mensagem precisa dizer o que instalar"

    def test_rota_existe_no_openapi(self, client_com_agente: TestClient) -> None:
        esquema = client_com_agente.get("/openapi.json").json()
        assert "/v1/agente/consultar" in esquema["paths"]

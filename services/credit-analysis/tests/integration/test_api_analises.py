"""Testes de integracao da API.

Sobem a aplicacao inteira (middleware, DI, handlers de erro, serializacao) e
exercitam o contrato HTTP. O que os unitarios nao pegam: wiring quebrado,
schema divergente e erro que escapa como 500.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


class TestHealth:
    def test_liveness(self, client: TestClient) -> None:
        resposta = client.get("/health")
        assert resposta.status_code == 200
        assert resposta.json()["status"] == "ok"

    def test_readiness(self, client: TestClient) -> None:
        assert client.get("/ready").status_code == 200

    def test_correlation_id_volta_no_header(self, client: TestClient) -> None:
        resposta = client.get("/health", headers={"X-Request-ID": "trace-123"})
        assert resposta.headers["X-Request-ID"] == "trace-123"

    def test_correlation_id_e_gerado_quando_ausente(self, client: TestClient) -> None:
        assert client.get("/health").headers["X-Request-ID"]


class TestCriarAnalise:
    def test_cria_e_devolve_parecer(
        self, client: TestClient, payload_analise: dict[str, Any]
    ) -> None:
        resposta = client.post("/v1/analises", json=payload_analise)
        assert resposta.status_code == 201

        corpo = resposta.json()
        assert corpo["status"] == "concluida"
        assert corpo["parecer"] is not None
        assert corpo["parecer"]["score"] >= 0
        assert corpo["parecer"]["justificativas"]

    def test_cpf_sai_mascarado(self, client: TestClient, payload_analise: dict[str, Any]) -> None:
        # Nenhuma resposta pode conter o CPF completo — vaza para log de proxy.
        corpo = client.post("/v1/analises", json=payload_analise).json()
        assert corpo["cpf_mascarado"] == "***.982.247-**"
        assert "52998224725" not in resposta_texto(corpo)

    def test_parcela_e_calculada_no_servidor(
        self, client: TestClient, payload_analise: dict[str, Any]
    ) -> None:
        corpo = client.post("/v1/analises", json=payload_analise).json()
        assert float(corpo["parcela_mensal"]) > 0

    def test_restricao_cadastral_resulta_em_negado(
        self, client_restrito: TestClient, payload_analise: dict[str, Any]
    ) -> None:
        corpo = client_restrito.post("/v1/analises", json=payload_analise).json()
        assert corpo["parecer"]["decisao"] == "negado"


class TestValidacao:
    def test_cpf_invalido_vira_422_e_nao_500(self, client: TestClient) -> None:
        payload = {
            "solicitante": {
                "nome": "Fulano de Tal",
                "cpf": "111.111.111-11",
                "data_nascimento": "1990-01-01T00:00:00Z",
                "renda_mensal_declarada": "5000.00",
            },
            "proposta": {
                "valor_solicitado": "10000.00",
                "prazo_meses": 12,
                "taxa_juros_mensal": "1.50",
            },
        }
        resposta = client.post("/v1/analises", json=payload)
        assert resposta.status_code == 422
        assert resposta.json()["codigo"] == "payload_invalido"

    @pytest.mark.parametrize(
        ("campo", "valor"),
        [
            ("prazo_meses", 0),
            ("prazo_meses", 500),
            ("valor_solicitado", "-100.00"),
            ("taxa_juros_mensal", "99"),
        ],
    )
    def test_proposta_fora_da_politica_e_rejeitada(
        self, client: TestClient, payload_analise: dict[str, Any], campo: str, valor: Any
    ) -> None:
        payload_analise["proposta"][campo] = valor
        resposta = client.post("/v1/analises", json=payload_analise)
        assert resposta.status_code == 422

    def test_erro_de_validacao_aponta_o_campo(
        self, client: TestClient, payload_analise: dict[str, Any]
    ) -> None:
        payload_analise["proposta"]["prazo_meses"] = 999
        detalhes = client.post("/v1/analises", json=payload_analise).json()["detalhes"]
        assert any("prazo_meses" in d["campo"] for d in detalhes)

    def test_menor_de_idade_e_rejeitado(
        self, client: TestClient, payload_analise: dict[str, Any]
    ) -> None:
        payload_analise["solicitante"]["data_nascimento"] = "2015-01-01T00:00:00Z"
        resposta = client.post("/v1/analises", json=payload_analise)
        assert resposta.status_code == 422
        assert resposta.json()["codigo"] in {"valor_invalido", "payload_invalido"}


class TestConsultaEListagem:
    def test_consulta_por_id(self, client: TestClient, payload_analise: dict[str, Any]) -> None:
        criada = client.post("/v1/analises", json=payload_analise).json()
        resposta = client.get(f"/v1/analises/{criada['id']}")
        assert resposta.status_code == 200
        assert resposta.json()["id"] == criada["id"]

    def test_id_inexistente_vira_404_com_codigo(self, client: TestClient) -> None:
        resposta = client.get("/v1/analises/00000000-0000-0000-0000-000000000000")
        assert resposta.status_code == 404
        assert resposta.json()["codigo"] == "analise_nao_encontrada"

    def test_id_malformado_vira_422(self, client: TestClient) -> None:
        assert client.get("/v1/analises/nao-e-uuid").status_code == 422

    def test_listagem_pagina(self, client: TestClient, payload_analise: dict[str, Any]) -> None:
        for _ in range(3):
            client.post("/v1/analises", json=payload_analise)

        corpo = client.get("/v1/analises", params={"limite": 2, "offset": 0}).json()
        assert corpo["total"] == 3
        assert len(corpo["itens"]) == 2

    def test_listagem_ordena_do_mais_recente(
        self, client: TestClient, payload_analise: dict[str, Any]
    ) -> None:
        ids = [client.post("/v1/analises", json=payload_analise).json()["id"] for _ in range(3)]
        itens = client.get("/v1/analises").json()["itens"]
        assert itens[0]["id"] == ids[-1]


class TestContratoOpenAPI:
    def test_schema_e_gerado(self, client: TestClient) -> None:
        schema = client.get("/openapi.json").json()
        assert "/v1/analises" in schema["paths"]
        assert "/health" in schema["paths"]


def resposta_texto(corpo: dict[str, Any]) -> str:
    """Serializa a resposta para busca textual por dado sensivel."""
    import json

    return json.dumps(corpo, default=str)

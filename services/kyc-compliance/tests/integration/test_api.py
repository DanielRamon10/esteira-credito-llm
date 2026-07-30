"""Testes de integracao da API de triagem."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from kyc_compliance.api.app import criar_app
from kyc_compliance.config import Settings
from kyc_compliance.domain.triagem import EntradaRestritiva
from kyc_compliance.infrastructure.listas import ListasEmMemoria
from tests.conftest import CPF_DA_PEP, CPF_LIMPO, montar_cliente

pytestmark = pytest.mark.integration


@pytest.fixture
def client(settings_teste: Settings, entradas: list[EntradaRestritiva]) -> Iterator[TestClient]:
    app = criar_app(settings=settings_teste, listas=ListasEmMemoria(entradas, "teste"))
    with montar_cliente(app) as c:
        yield c


@pytest.fixture
def client_sem_listas(settings_teste: Settings) -> Iterator[TestClient]:
    """Lista vazia: o readiness precisa reprovar.

    Este e o cenario perigoso do dominio — sem lista o servico aprovaria todo
    mundo reportando "nenhuma correspondencia". Precisa sair do load balancer.
    """
    app = criar_app(settings=settings_teste, listas=ListasEmMemoria([], "vazia"))
    with montar_cliente(app) as c:
        yield c


class TestTriagem:
    def test_aprova_quem_nao_esta_em_lista(self, client: TestClient) -> None:
        resposta = client.post(
            "/v1/triagens", json={"nome": "Beatriz Nogueira Prado", "cpf": CPF_LIMPO}
        )
        assert resposta.status_code == 201

        corpo = resposta.json()
        assert corpo["decisao"] == "aprovado"
        assert corpo["aprovado"] is True
        assert corpo["correspondencias"] == []
        assert corpo["entradas_avaliadas"] > 0

    def test_pep_sai_como_aprovado_com_diligencia(self, client: TestClient) -> None:
        resposta = client.post(
            "/v1/triagens", json={"nome": "Maria Fernanda Souza", "cpf": CPF_DA_PEP}
        )

        corpo = resposta.json()
        assert corpo["decisao"] == "aprovado_com_diligencia"
        assert corpo["aprovado"] is True, "o consumidor nao pode ler PEP como recusa"

    def test_sancao_sai_como_reprovado(self, client: TestClient) -> None:
        resposta = client.post(
            "/v1/triagens", json={"nome": "Jose Antonio Pereira", "cpf": CPF_LIMPO}
        )

        corpo = resposta.json()
        assert corpo["decisao"] == "reprovado"
        assert corpo["aprovado"] is False
        assert corpo["nivel_risco"] == "inaceitavel"

    def test_resposta_expoe_a_explicacao(self, client: TestClient) -> None:
        # Contrato precisa carregar o porque: quem consome tem de poder explicar a
        # decisao a um analista e a um regulador.
        resposta = client.post(
            "/v1/triagens", json={"nome": "Carlos Eduardo Lima", "cpf": CPF_LIMPO}
        )

        correspondencia = resposta.json()["correspondencias"][0]
        assert correspondencia["nome_na_lista"] == "CARLOS E. LIMA"
        assert correspondencia["tokens_casados"]
        assert "CARLOS" in correspondencia["justificativa"]
        assert 0 <= correspondencia["score"] <= 1

    def test_cpf_completo_nunca_sai_na_resposta(self, client: TestClient) -> None:
        resposta = client.post(
            "/v1/triagens", json={"nome": "Maria Fernanda Souza", "cpf": CPF_DA_PEP}
        )

        texto = resposta.text
        assert CPF_DA_PEP not in texto
        assert "52998224725" not in texto
        assert resposta.json()["cpf_mascarado"] == "***.982.247-**"


class TestValidacao:
    def test_cpf_com_digito_verificador_errado_e_422(self, client: TestClient) -> None:
        resposta = client.post("/v1/triagens", json={"nome": "Ana Souza", "cpf": "111.111.111-11"})

        assert resposta.status_code == 422
        assert resposta.json()["codigo"] == "payload_invalido"

    def test_nome_curto_e_422(self, client: TestClient) -> None:
        assert client.post("/v1/triagens", json={"nome": "Jo", "cpf": CPF_LIMPO}).status_code == 422

    def test_cpf_aceita_com_e_sem_pontuacao(self, client: TestClient) -> None:
        for cpf in (CPF_LIMPO, CPF_LIMPO.replace(".", "").replace("-", "")):
            resposta = client.post("/v1/triagens", json={"nome": "Beatriz Prado", "cpf": cpf})
            assert resposta.status_code == 201


class TestConsultaEListagem:
    def test_consulta_por_id(self, client: TestClient) -> None:
        criada = client.post("/v1/triagens", json={"nome": "Beatriz Prado", "cpf": CPF_LIMPO})
        triagem_id = criada.json()["id"]

        resposta = client.get(f"/v1/triagens/{triagem_id}")

        assert resposta.status_code == 200
        assert resposta.json()["id"] == triagem_id

    def test_id_inexistente_e_404(self, client: TestClient) -> None:
        resposta = client.get("/v1/triagens/00000000-0000-0000-0000-000000000000")
        assert resposta.status_code == 404

    def test_listagem_traz_a_mais_recente_primeiro(self, client: TestClient) -> None:
        # O desempate por ordem de insercao existe porque o relogio do Windows tem
        # resolucao de ~15ms: sem ele a ordenacao sairia invertida.
        for nome in ("Primeira Pessoa Aqui", "Segunda Pessoa Aqui", "Terceira Pessoa Aqui"):
            client.post("/v1/triagens", json={"nome": nome, "cpf": CPF_LIMPO})

        itens = client.get("/v1/triagens", params={"limite": 3}).json()["itens"]

        assert itens[0]["nome_consultado"] == "Terceira Pessoa Aqui"
        assert itens[-1]["nome_consultado"] == "Primeira Pessoa Aqui"


class TestSondas:
    def test_health_nao_depende_de_lista(self, client_sem_listas: TestClient) -> None:
        # Liveness responde "o processo esta vivo?". Falhar aqui causaria restart
        # loop sem resolver nada.
        assert client_sem_listas.get("/health").status_code == 200

    def test_health_omite_a_contagem_em_vez_de_afirmar_zero(self, client: TestClient) -> None:
        """Regressao encontrada rodando o pod num cluster de verdade, nao pela suite.

        Com `entradas_carregadas: int = 0` no schema, o `/health` afirmava zero entradas
        num pod com 15 carregadas. Zero e a condicao mais grave deste dominio — o servico
        aprovando todo mundo por falta de lista —, e quem lesse a sonda durante um
        incidente concluiria o oposto da verdade.

        A sonda de liveness nao consulta o repositorio de proposito, logo ela nunca teve
        esse numero. Omitir e honesto; preencher com o default nao era.
        """
        corpo = client.get("/health").json()

        assert "entradas_carregadas" not in corpo
        assert "procedencia_listas" not in corpo
        # E o /ready, que de fato consulta, continua respondendo o numero real.
        assert client.get("/ready").json()["entradas_carregadas"] > 0

    def test_ready_reprova_com_lista_vazia(self, client_sem_listas: TestClient) -> None:
        resposta = client_sem_listas.get("/ready")

        assert resposta.status_code == 503
        assert resposta.json()["status"] == "degradado"
        assert resposta.json()["entradas_carregadas"] == 0

    def test_ready_aprova_com_lista_carregada(self, client: TestClient) -> None:
        resposta = client.get("/ready")

        assert resposta.status_code == 200
        assert resposta.json()["entradas_carregadas"] > 0
        assert resposta.json()["procedencia_listas"] == "teste"


class TestCorrelacao:
    def test_reaproveita_o_request_id_do_chamador(self, client: TestClient) -> None:
        """O que permite seguir uma analise de credito que consultou o KYC.

        Gerar um id novo aqui quebraria a correlacao exatamente onde ela e util:
        os dois servicos precisam logar o mesmo identificador.
        """
        recebido = "id-vindo-do-credit-analysis"
        resposta = client.post(
            "/v1/triagens",
            json={"nome": "Beatriz Prado", "cpf": CPF_LIMPO},
            headers={"X-Request-ID": recebido},
        )

        assert resposta.headers["X-Request-ID"] == recebido

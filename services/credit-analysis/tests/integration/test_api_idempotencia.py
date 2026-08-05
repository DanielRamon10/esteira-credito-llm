"""Idempotencia pela rota: `POST /v1/analises` com `Idempotency-Key`.

## O que esta suite mede que a de repositorio nao mede

`test_idempotencia.py` verifica a reivindicacao contra Postgres — corrida, abandono, janela. Aqui a
pergunta e outra: **a rota usa aquilo direito?** Um adapter perfeito ligado errado produz o mesmo
defeito de antes, e nenhum teste de repositorio pega isso.

A assercao central e `test_repeticao_nao_cria_segunda_analise`, e ela conta analises no repositorio
em vez de comparar respostas: duas chamadas poderiam devolver corpos iguais e ainda assim ter criado
dois registros — se a segunda resposta viesse de um cache, por exemplo.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from credit_analysis.api.app import criar_app
from credit_analysis.config import Settings
from credit_analysis.infrastructure.bureau import BureauSempreLimpo
from credit_analysis.infrastructure.repositories.memoria import RepositorioAnalisesMemoria
from tests.conftest import emitir_token, montar_cliente

pytestmark = pytest.mark.integration


@pytest.fixture
def cliente(
    settings_teste: Settings, repositorio: RepositorioAnalisesMemoria, chaves_de_teste: Path
) -> Iterator[TestClient]:
    app = criar_app(settings=settings_teste, repositorio=repositorio, bureau=BureauSempreLimpo())
    with montar_cliente(app, emitir_token(chaves_de_teste)) as c:
        yield c


@pytest.fixture
def cliente_cru(
    settings_teste: Settings, repositorio: RepositorioAnalisesMemoria, chaves_de_teste: Path
) -> Iterator[TestClient]:
    """Cliente **sem** o gancho que injeta a chave.

    `montar_cliente` poe uma chave em toda requisicao, o que faz a suite inteira funcionar sem
    edicao — e esconderia a exigencia. Este cliente existe para o teste que a verifica.
    """
    app = criar_app(settings=settings_teste, repositorio=repositorio, bureau=BureauSempreLimpo())
    with TestClient(app, headers={"Authorization": f"Bearer {emitir_token(chaves_de_teste)}"}) as c:
        yield c


def payload(valor: str = "45000.00") -> dict[str, Any]:
    return {
        "solicitante": {
            "nome": "Maria Oliveira Santos",
            "cpf": "529.982.247-25",
            "data_nascimento": "1990-05-14",
            "renda_mensal_declarada": "8500.00",
        },
        "proposta": {
            "valor_solicitado": valor,
            "prazo_meses": 36,
            "taxa_juros_mensal": "1.99",
        },
    }


class TestChaveObrigatoria:
    def test_sem_chave_de_idempotencia_e_400(self, cliente_cru: TestClient) -> None:
        """A exigencia em si, com um cliente que nao ajuda.

        400 e nao 428: o `draft-ietf-httpapi-idempotency-key-header` especifica 400 para chave
        ausente, e 428 fala de requisicao condicional — outra coisa.
        """
        resposta = cliente_cru.post("/v1/analises", json=payload())

        assert resposta.status_code == 400, resposta.text
        corpo = resposta.json()
        assert corpo["codigo"] == "chave_de_idempotencia_ausente"
        # A mensagem diz o que fazer: um 400 que so nomeia o cabecalho manda o cliente adivinhar
        # se a chave deve mudar ou repetir entre chamadas — e as duas leituras sao plausiveis.
        assert "mesmo" in corpo["mensagem"]

    def test_chave_longa_demais_e_422(self, cliente_cru: TestClient) -> None:
        """O limite existe para a chave nao virar campo livre: ela e indexada e registrada."""
        resposta = cliente_cru.post(
            "/v1/analises", json=payload(), headers={"Idempotency-Key": "x" * 300}
        )

        assert resposta.status_code == 422, resposta.text


class TestRepeticao:
    async def test_repeticao_nao_cria_segunda_analise(
        self, cliente: TestClient, repositorio: RepositorioAnalisesMemoria
    ) -> None:
        """A assercao que define a camada, e ela conta **registros**, nao respostas.

        Duas respostas iguais nao provam que houve uma analise so: um cache de resposta daria o
        mesmo corpo com dois registros no banco. Contar no repositorio mede o que importa.
        """
        chave = {"Idempotency-Key": str(uuid4())}

        primeira = cliente.post("/v1/analises", json=payload(), headers=chave)
        segunda = cliente.post("/v1/analises", json=payload(), headers=chave)

        assert primeira.status_code == 201, primeira.text
        assert segunda.status_code == 200, segunda.text
        assert primeira.json()["id"] == segunda.json()["id"]
        assert await repositorio.contar() == 1

    async def test_chaves_diferentes_criam_analises_diferentes(
        self, cliente: TestClient, repositorio: RepositorioAnalisesMemoria
    ) -> None:
        """O par negativo. Uma rota que sempre devolvesse a primeira analise passaria no de cima.

        Duas submissoes legitimas do mesmo solicitante — ele pediu outro emprestimo — precisam
        produzir duas analises.
        """
        primeira = cliente.post(
            "/v1/analises", json=payload(), headers={"Idempotency-Key": str(uuid4())}
        )
        segunda = cliente.post(
            "/v1/analises", json=payload(), headers={"Idempotency-Key": str(uuid4())}
        )

        assert primeira.json()["id"] != segunda.json()["id"]
        assert await repositorio.contar() == 2

    def test_201_na_primeira_e_200_na_repeticao(self, cliente: TestClient) -> None:
        """O codigo distingue "criei agora" de "ja existia", e o cliente precisa dessa distincao.

        Devolver 201 nas duas faria um cliente que conta criacoes contar duas.
        """
        chave = {"Idempotency-Key": str(uuid4())}

        assert cliente.post("/v1/analises", json=payload(), headers=chave).status_code == 201
        assert cliente.post("/v1/analises", json=payload(), headers=chave).status_code == 200

    def test_mesma_chave_com_pedido_diferente_e_422(self, cliente: TestClient) -> None:
        """O caso que faz a impressao do pedido valer a pena.

        Sem comparar o corpo, o cliente que reusa a chave por engano — fixada por sessao, por
        exemplo — receberia a resposta do primeiro pedido e concluiria que submeteu uma analise de
        R$ 80.000 quando submeteu a de R$ 45.000.
        """
        chave = {"Idempotency-Key": str(uuid4())}
        cliente.post("/v1/analises", json=payload("45000.00"), headers=chave)

        resposta = cliente.post("/v1/analises", json=payload("80000.00"), headers=chave)

        assert resposta.status_code == 422, resposta.text
        assert resposta.json()["codigo"] == "chave_de_idempotencia_reusada"

    def test_ordem_das_chaves_do_json_nao_gera_conflito(self, cliente: TestClient) -> None:
        """Mesmo pedido serializado noutra ordem continua sendo o mesmo pedido.

        Sem canonicalizar a impressao, um cliente com dicionario nao ordenado veria o proprio retry
        virar 422 — e o erro apontaria para "pedido diferente" quando nada mudou.
        """
        chave = {"Idempotency-Key": str(uuid4())}
        corpo = payload()
        cliente.post("/v1/analises", json=corpo, headers=chave)

        invertido = {"proposta": corpo["proposta"], "solicitante": corpo["solicitante"]}
        resposta = cliente.post("/v1/analises", json=invertido, headers=chave)

        assert resposta.status_code == 200, resposta.text


class TestInteracaoComApagamento:
    """O ponto onde a Camada 11 poderia ter quebrado a Camada 10 em silencio."""

    def test_repeticao_de_analise_apagada_nao_a_ressuscita(
        self, cliente: TestClient, repositorio: RepositorioAnalisesMemoria
    ) -> None:
        """Se o registro guardasse o corpo, esta chamada devolveria dado que foi excluido.

        Guardando o id, a repeticao le o recurso — e nao acha. O 404 e a verdade, e ele cai fora do
        desenho sem precisar de uma regra dizendo "nao devolva dado apagado".

        Aqui o apagamento e feito direto no repositorio: o que se mede e a rota de criacao diante de
        um recurso ausente, e nao a rota de privacidade (que tem suite propria e exige Postgres).
        """
        chave = {"Idempotency-Key": str(uuid4())}
        criada = cliente.post("/v1/analises", json=payload(), headers=chave)
        analise_id = UUID(criada.json()["id"])

        repositorio._itens.pop(analise_id)

        resposta = cliente.post("/v1/analises", json=payload(), headers=chave)

        assert resposta.status_code == 404, resposta.text
        assert str(analise_id) in resposta.json()["mensagem"]

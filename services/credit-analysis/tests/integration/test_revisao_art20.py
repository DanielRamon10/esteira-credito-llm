"""Pedido de revisao de decisao automatizada (LGPD art. 20).

## O que esta suite protege

O art. 20 tem duas metades, e a rota cumpre as duas na mesma resposta: o caput da o direito de pedir
revisao, o §1 obriga a informar os criterios. Um endpoint que so registrasse o pedido cumpriria
metade e obrigaria uma segunda chamada para a informacao a que o titular ja tinha direito.

As tres assercoes que definem o desenho:

1. **o pedido nao muda a decisao** — aprovar automaticamente por contestacao seria absurdo, negar
   seria pior;
2. **o pedido nao consome reavaliacao** — o teto de cinco existe para impedir que alguem reenvie
   documento ate obter o parecer que quer, e gastar aquele teto num direito seria limitar o direito;
3. **o prazo conta do primeiro pedido** — reenviar nao reinicia, senao o controlador teria como
   empurrar o prazo para frente usando o pedido do proprio titular.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg_pool import AsyncConnectionPool

from credit_analysis.api.app import criar_app
from credit_analysis.config import Settings, get_settings
from credit_analysis.domain.entities import MAX_REAVALIACOES
from credit_analysis.infrastructure.bureau import BureauSempreLimpo
from credit_analysis.infrastructure.llm.anthropic_adapter import LLMFake
from credit_analysis.infrastructure.repositories.postgres import (
    _VERSOES,
    RepositorioAnalisesPostgres,
)
from tests.conftest import emitir_token, montar_cliente

SUFIXO_TESTE = "_test"


def _dsn_de_teste() -> str:
    bruto = os.getenv("CREDIT_POSTGRES_DSN_TEST") or get_settings().postgres_dsn.strip()
    if not bruto:
        return ""
    partes = urlsplit(bruto)
    banco = partes.path.lstrip("/")
    if not banco.endswith(SUFIXO_TESTE):
        banco += SUFIXO_TESTE
    return urlunsplit(partes._replace(path=f"/{banco}"))


DSN = _dsn_de_teste()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="CREDIT_POSTGRES_DSN nao definido"),
]


@pytest.fixture
async def pool() -> AsyncIterator[AsyncConnectionPool]:
    p = AsyncConnectionPool(DSN, min_size=1, max_size=4, open=False)
    await p.open(wait=True, timeout=15)
    async with p.connection() as conexao:
        await conexao.execute("DELETE FROM analise")
        await conexao.execute("DELETE FROM decisao_retida")
        await conexao.execute("DELETE FROM idempotencia")
    _VERSOES.clear()
    yield p
    await p.close()


@pytest.fixture
def cliente(
    settings_teste: Settings, chaves_de_teste: Path, pool: AsyncConnectionPool
) -> Iterator[TestClient]:
    app = criar_app(
        settings=settings_teste,
        repositorio=RepositorioAnalisesPostgres(pool),
        bureau=BureauSempreLimpo(),
        llm=LLMFake(),
    )
    with montar_cliente(app, emitir_token(chaves_de_teste)) as c:
        yield c


def payload() -> dict[str, Any]:
    return {
        "solicitante": {
            "nome": "Ana Souza",
            "cpf": "529.982.247-25",
            "data_nascimento": "1990-05-14",
            "renda_mensal_declarada": "8500.00",
        },
        "proposta": {
            "valor_solicitado": "45000.00",
            "prazo_meses": 36,
            "taxa_juros_mensal": "1.99",
        },
    }


def criar(cliente: TestClient) -> dict[str, Any]:
    resposta = cliente.post(
        "/v1/analises", json=payload(), headers={"Idempotency-Key": str(uuid4())}
    )
    assert resposta.status_code == 201, resposta.text
    return dict(resposta.json())


class TestPedidoDeRevisao:
    def test_registra_o_pedido_e_devolve_os_criterios(self, cliente: TestClient) -> None:
        """As duas metades do art. 20 na mesma resposta."""
        analise = criar(cliente)

        resposta = cliente.post(f"/v1/analises/{analise['id']}/revisao")

        assert resposta.status_code == 202, resposta.text
        corpo = resposta.json()
        assert corpo["analise_id"] == analise["id"]
        assert corpo["primeiro_pedido"] is True
        # O §1: criterios compreensiveis, na linguagem da politica.
        assert corpo["criterios"], "resposta sem os criterios da decisao"
        assert corpo["politicas_aplicadas"]
        assert "art. 20" in corpo["base_legal"]

    def test_o_pedido_nao_muda_a_decisao(self, cliente: TestClient) -> None:
        """Contestar nao aprova nem nega — so tira do caminho automatico.

        Sem esta assercao, uma implementacao que "resolvesse" o pedido mudando o parecer passaria
        nos outros testes: o pedido estaria registrado e os criterios devolvidos.
        """
        analise = criar(cliente)
        antes = cliente.get(f"/v1/analises/{analise['id']}").json()

        cliente.post(f"/v1/analises/{analise['id']}/revisao")
        depois = cliente.get(f"/v1/analises/{analise['id']}").json()

        assert depois["parecer"]["decisao"] == antes["parecer"]["decisao"]
        assert depois["parecer"]["score"] == antes["parecer"]["score"]
        assert depois["status"] == antes["status"]

    async def test_o_pedido_nao_consome_reavaliacao(
        self, cliente: TestClient, pool: AsyncConnectionPool
    ) -> None:
        """O teto de cinco reaberturas existe para outra coisa.

        Ele impede que alguem reenvie documento indefinidamente ate obter o parecer que quer. Gastar
        aquele teto num pedido de revisao limitaria um **direito** a cinco usos — e um titular que
        contestasse cinco vezes perderia a capacidade de anexar documento novo.

        A contagem e lida no banco e nao na resposta da API: `reavaliacoes` nao esta no schema
        publico, e expo-lo so para este teste seria mudar o contrato por causa de um teste.
        """
        analise = criar(cliente)

        for _ in range(MAX_REAVALIACOES + 2):
            resposta = cliente.post(f"/v1/analises/{analise['id']}/revisao")
            assert resposta.status_code == 202, resposta.text

        async with pool.connection() as conexao:
            cursor = await conexao.execute(
                "SELECT reavaliacoes, status FROM analise WHERE id = %s", (analise["id"],)
            )
            linha = await cursor.fetchone()

        assert linha is not None
        assert linha[0] == 0, "pedido de revisao consumiu reavaliacao"
        # E o ciclo de vida nao se moveu: `PROCESSANDO` diria que algo esta sendo calculado.
        assert linha[1] == "concluida"

    def test_reenvio_nao_reinicia_o_prazo(self, cliente: TestClient) -> None:
        """A data do primeiro pedido e a que vale.

        Atualiza-la a cada reenvio daria ao controlador um jeito de empurrar o prazo de resposta
        para frente usando o pedido do proprio titular.
        """
        analise = criar(cliente)

        primeira = cliente.post(f"/v1/analises/{analise['id']}/revisao").json()
        segunda = cliente.post(f"/v1/analises/{analise['id']}/revisao").json()

        assert primeira["primeiro_pedido"] is True
        assert segunda["primeiro_pedido"] is False
        assert segunda["pedido_registrado_em"] == primeira["pedido_registrado_em"]

    def test_analise_inexistente_e_404(self, cliente: TestClient) -> None:
        assert cliente.post(f"/v1/analises/{uuid4()}/revisao").status_code == 404

    def test_exige_escopo_de_escrita(
        self, settings_teste: Settings, chaves_de_teste: Path, pool: AsyncConnectionPool
    ) -> None:
        """Registrar contestacao muda estado, e leitura nao muda estado."""
        app = criar_app(
            settings=settings_teste,
            repositorio=RepositorioAnalisesPostgres(pool),
            bureau=BureauSempreLimpo(),
            llm=LLMFake(),
        )
        with montar_cliente(app, emitir_token(chaves_de_teste, escopos=("analises:ler",))) as c:
            resposta = c.post(f"/v1/analises/{uuid4()}/revisao")

        assert resposta.status_code == 403, resposta.text


class TestTrilhaDoPedido:
    async def test_o_pedido_sobrevive_ao_restart(
        self, cliente: TestClient, pool: AsyncConnectionPool
    ) -> None:
        """Contestacao registrada em memoria seria contestacao perdida no deploy seguinte."""
        analise = criar(cliente)
        cliente.post(f"/v1/analises/{analise['id']}/revisao")

        async with pool.connection() as conexao:
            cursor = await conexao.execute(
                "SELECT revisao_solicitada_em, revisao_solicitada_por FROM analise WHERE id = %s",
                (analise["id"],),
            )
            linha = await cursor.fetchone()

        assert linha is not None
        assert linha[0] is not None
        # O canal, e nao o titular: quem pede e sempre ele, e repetir seria dado pessoal a mais.
        assert linha[1]

    async def test_a_contestacao_sobrevive_ao_apagamento_dos_identificadores(
        self, cliente: TestClient, pool: AsyncConnectionPool
    ) -> None:
        """Cruzamento com a Camada 10, e o motivo de `decisao_retida` ter a coluna.

        Um pedido de exclusao (art. 18) leva a analise. Se a contestacao fosse embora com ela, uma
        auditoria de "quantas decisoes foram contestadas?" perderia o caso — e essa pergunta e de
        politica, nao de titular.

        Booleano e nao data no registro conservado: o prazo so importa enquanto o caso existe, e a
        data seria um quasi-identificador a mais numa tabela feita para nao ter nenhum.
        """
        analise = criar(cliente)
        cliente.post(f"/v1/analises/{analise['id']}/revisao")

        apagamento = cliente.post("/v1/privacidade/apagamentos", json={"cpf": "529.982.247-25"})
        assert apagamento.status_code == 200, apagamento.text

        async with pool.connection() as conexao:
            cursor = await conexao.execute(
                "SELECT revisao_solicitada FROM decisao_retida WHERE analise_id = %s",
                (analise["id"],),
            )
            linha = await cursor.fetchone()

        assert linha is not None
        assert linha[0] is True

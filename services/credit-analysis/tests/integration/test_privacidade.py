"""Direitos do titular pela API, contra Postgres real.

## Por que estes testes exigem o banco

A rota existe para remover dado **duravel**. Contra o repositorio em memoria ela responde 503 por
construcao, e um teste que so exercitasse esse caminho verificaria a recusa e nunca o atendimento.

O par 503/200 esta aqui: um teste para o ambiente sem persistencia, o resto contra Postgres.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from psycopg_pool import AsyncConnectionPool

from credit_analysis.api.app import criar_app
from credit_analysis.config import Settings, get_settings
from credit_analysis.infrastructure.bureau import BureauSempreLimpo
from credit_analysis.infrastructure.llm.anthropic_adapter import LLMFake
from credit_analysis.infrastructure.repositories.memoria import RepositorioAnalisesMemoria
from credit_analysis.infrastructure.repositories.postgres import (
    _VERSOES,
    RepositorioAnalisesPostgres,
)
from tests.conftest import emitir_token, montar_cliente

SUFIXO_TESTE = "_test"


def _dsn_de_teste() -> str:
    """Mesma disciplina do `test_repositorio_postgres`: nunca o banco de desenvolvimento."""
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
    pytest.mark.skipif(
        not DSN, reason="CREDIT_POSTGRES_DSN nao definido; suba o banco com `docker compose up -d`"
    ),
]


@pytest.fixture
async def pool() -> AsyncIterator[AsyncConnectionPool]:
    p = AsyncConnectionPool(DSN, min_size=1, max_size=4, open=False)
    await p.open(wait=True, timeout=15)
    async with p.connection() as conexao:
        await conexao.execute("DELETE FROM analise")
        await conexao.execute("DELETE FROM decisao_retida")
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


def payload(cpf: str = "529.982.247-25", valor: str = "45000.00") -> dict[str, Any]:
    return {
        "solicitante": {
            "nome": "Maria Oliveira Santos",
            "cpf": cpf,
            "data_nascimento": "1990-05-14",
            "renda_mensal_declarada": "8500.00",
        },
        "proposta": {
            "valor_solicitado": valor,
            "prazo_meses": 36,
            "taxa_juros_mensal": "1.99",
        },
    }


class TestApagamentoPelaAPI:
    def test_apaga_e_devolve_recibo(self, cliente: TestClient) -> None:
        criada = cliente.post("/v1/analises", json=payload())
        assert criada.status_code == 201, criada.text
        analise_id = criada.json()["id"]

        resposta = cliente.post("/v1/privacidade/apagamentos", json={"cpf": "529.982.247-25"})

        assert resposta.status_code == 200, resposta.text
        corpo = resposta.json()
        assert corpo["analises_afetadas"] == [analise_id]
        assert corpo["decisoes_conservadas"] == 1
        assert "art. 16" in corpo["base_legal"]

        # E o dado foi de fato embora.
        assert cliente.get(f"/v1/analises/{analise_id}").status_code == 404

    def test_o_recibo_nao_devolve_o_cpf(self, cliente: TestClient) -> None:
        """A assercao mais importante deste arquivo, e ela e sobre o corpo inteiro.

        Conferir campo por campo passaria enquanto ninguem acrescentasse um `cpf` "para
        conferencia". Procurar no texto bruto da resposta pega qualquer forma — pontuada, sem
        pontuacao, dentro de uma mensagem, dentro de um campo novo que alguem adicionou sem pensar
        nisto.

        O motivo: a resposta pode ser registrada por proxy, cache e cliente, e este e o atendimento
        de um pedido para remover exatamente aquele dado.
        """
        cliente.post("/v1/analises", json=payload())

        resposta = cliente.post("/v1/privacidade/apagamentos", json={"cpf": "529.982.247-25"})

        bruto = resposta.text
        assert "529.982.247-25" not in bruto
        assert "52998224725" not in bruto
        assert "Maria" not in bruto

    def test_alcanca_todas_as_analises_do_titular(self, cliente: TestClient) -> None:
        """Pedido de exclusao nao e "apague a mais recente"."""
        primeira = cliente.post("/v1/analises", json=payload()).json()["id"]
        segunda = cliente.post("/v1/analises", json=payload(valor="80000.00")).json()["id"]
        de_outro = cliente.post("/v1/analises", json=payload(cpf="111.444.777-35")).json()["id"]

        corpo = cliente.post("/v1/privacidade/apagamentos", json={"cpf": "529.982.247-25"}).json()

        assert set(corpo["analises_afetadas"]) == {primeira, segunda}
        # A analise de outra pessoa continua intacta: um pedido nao pode transbordar de titular.
        assert cliente.get(f"/v1/analises/{de_outro}").status_code == 200

    def test_cpf_sem_analise_devolve_200_e_lista_vazia(self, cliente: TestClient) -> None:
        """404 aqui seria um oraculo de existencia.

        Com escopo de escrita e uma lista de CPFs, um 404 distinguiria quem tem cadastro de quem nao
        tem — informacao que a rota criada para proteger a pessoa entregaria de graca.
        """
        resposta = cliente.post("/v1/privacidade/apagamentos", json={"cpf": "111.444.777-35"})

        assert resposta.status_code == 200
        assert resposta.json()["analises_afetadas"] == []
        assert resposta.json()["decisoes_conservadas"] == 0

    def test_cpf_invalido_e_422(self, cliente: TestClient) -> None:
        """DV conferido antes de varrer o banco.

        Sem isto, um CPF digitado errado no atendimento provocaria uma varredura completa e um
        recibo de "nada encontrado" — que quem atende leria como "esta pessoa nao tem cadastro".
        """
        resposta = cliente.post("/v1/privacidade/apagamentos", json={"cpf": "111.111.111-11"})

        assert resposta.status_code == 422, resposta.text

    def test_pedido_reenviado_nao_estoura(self, cliente: TestClient) -> None:
        """Pedido de titular chega por canal humano; reenvio e normal."""
        cliente.post("/v1/analises", json=payload())

        primeira = cliente.post("/v1/privacidade/apagamentos", json={"cpf": "529.982.247-25"})
        segunda = cliente.post("/v1/privacidade/apagamentos", json={"cpf": "529.982.247-25"})

        assert primeira.status_code == 200
        assert segunda.status_code == 200
        assert len(primeira.json()["analises_afetadas"]) == 1
        assert segunda.json()["analises_afetadas"] == []

    def test_exige_escopo_de_escrita(
        self, settings_teste: Settings, chaves_de_teste: Path, pool: AsyncConnectionPool
    ) -> None:
        """Leitura nao destroi. Um token de `analises:ler` recebe 403 aqui."""
        app = criar_app(
            settings=settings_teste,
            repositorio=RepositorioAnalisesPostgres(pool),
            bureau=BureauSempreLimpo(),
            llm=LLMFake(),
        )
        token = emitir_token(chaves_de_teste, escopos=("analises:ler",))
        with montar_cliente(app, token) as c:
            resposta = c.post("/v1/privacidade/apagamentos", json={"cpf": "529.982.247-25"})

        assert resposta.status_code == 403, resposta.text


class TestAmbienteSemPersistencia:
    def test_repositorio_em_memoria_responde_503(
        self, settings_teste: Settings, chaves_de_teste: Path
    ) -> None:
        """503 com instrucao, e nao 200 fingindo que apagou.

        O adapter em memoria "apagaria" um dicionario e devolveria recibo — o titular receberia
        comprovante de exclusao de um sistema que nao conserva nada, e o comprovante seria falso
        sobre o que existia.
        """
        app = criar_app(
            settings=settings_teste,
            repositorio=RepositorioAnalisesMemoria(),
            bureau=BureauSempreLimpo(),
            llm=LLMFake(),
        )
        with montar_cliente(app, emitir_token(chaves_de_teste)) as c:
            resposta = c.post("/v1/privacidade/apagamentos", json={"cpf": "529.982.247-25"})

        assert resposta.status_code == 503, resposta.text
        # A mensagem diz como habilitar: 503 sem instrucao manda quem opera ler o codigo.
        assert "CREDIT_POSTGRES_DSN" in resposta.json()["mensagem"]


class TestTrilhaDoAtendimento:
    async def test_a_prova_do_atendimento_fica_no_banco(
        self, cliente: TestClient, pool: AsyncConnectionPool
    ) -> None:
        """O controlador precisa demonstrar que atendeu, e log nao e demonstracao.

        `motivo` e `identificacao_removida_em` sao a prova, com controle de acesso do banco. Um log
        serviria enquanto a retencao do agregador durasse — e o prazo dele nao e o da obrigacao.
        """
        cliente.post("/v1/analises", json=payload())
        antes = datetime.now(UTC)

        corpo = cliente.post("/v1/privacidade/apagamentos", json={"cpf": "529.982.247-25"}).json()

        analise_id = UUID(corpo["analises_afetadas"][0])
        async with pool.connection() as conexao:
            cursor = await conexao.execute(
                "SELECT motivo, identificacao_removida_em FROM decisao_retida"
                " WHERE analise_id = %s",
                (analise_id,),
            )
            linha = await cursor.fetchone()

        assert linha is not None
        assert linha[0] == "pedido_do_titular"
        assert linha[1] >= antes

    def test_nenhum_log_do_ciclo_de_vida_recebe_dado_pessoal(self) -> None:
        """Registrar o CPF no log deixaria o dado onde a pessoa pediu para nao estar.

        Log vai para agregador, com retencao propria e outro controle de acesso — frequentemente
        nenhum. O evento precisa dizer que houve atendimento, e nao de quem.

        ## Por que estatico, depois de quatro tentativas dinamicas falharem

        1. `structlog.testing.capture_logs` em volta da chamada HTTP: o `TestClient` roda a
           requisicao noutra thread, e a captura e por contexto. Zero registros;
        2. o mesmo, chamando o caso de uso direto no contexto do teste. Tambem zero —
           `configurar_logging` usa `cache_logger_on_first_use=True`, e logger ja resolvido ignora
           processador trocado depois;
        3. `caplog`, que intercepta no `logging` da stdlib. Ainda zero: `settings_teste` usa
           `nivel_log="WARNING"`, e `make_filtering_bound_logger` descarta o INFO antes de a stdlib
           ver qualquer coisa;
        4. `caplog` com `configurar_logging("INFO")` antes. Zero de novo, pela causa (2): o logger
           de modulo do caso de uso ja tinha sido cacheado com WARNING por um `criar_app` anterior
           na mesma sessao.

        **As quatro versoes passariam com o CPF no log** — afirmavam sobre lista vazia. Foi o
        `assert len(...) == 1` que denunciou; sem ele, teria ficado um teste verde sem conteudo.

        A verificacao estatica nao depende de nivel, de thread nem de ordem de configuracao, e pega
        exatamente a regressao que importa: alguem acrescentar `cpf=...` a uma chamada de log para
        facilitar depuracao. Ela nao prova o que sai em producao; isso foi conferido a mao contra o
        log do container (`docker compose logs | grep <cpf>`, vazio), e esta anotado no README.
        """
        import ast
        import inspect

        from credit_analysis.application.use_cases import ciclo_de_vida

        arvore = ast.parse(inspect.getsource(ciclo_de_vida))
        proibidos = {"cpf", "nome", "titular", "solicitante", "documento", "texto"}
        achados: list[str] = []

        for no in ast.walk(arvore):
            if not isinstance(no, ast.Call):
                continue
            alvo = no.func
            # Somente chamadas `logger.<algo>(...)`.
            if not (
                isinstance(alvo, ast.Attribute)
                and isinstance(alvo.value, ast.Name)
                and alvo.value.id == "logger"
            ):
                continue
            for argumento in no.keywords:
                if argumento.arg and argumento.arg.lower() in proibidos:
                    achados.append(f"linha {no.lineno}: {argumento.arg}")

        assert not achados, f"dado pessoal em log de ciclo de vida: {achados}"

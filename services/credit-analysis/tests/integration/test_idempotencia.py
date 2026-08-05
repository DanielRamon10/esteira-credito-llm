"""Reivindicacao de chave de idempotencia, contra Postgres real.

## O teste que justifica a camada

`TestCorrida::test_duas_reivindicacoes_simultaneas_so_uma_ganha`. Ele exercita o caso que a
idempotencia existe para cobrir — o clique duplo, que chega **junto** — e que o desenho ingenuo
(`SELECT` e depois `INSERT`) nao cobre.

Um teste sequencial passaria com o desenho ingenuo: a primeira chamada insere, a segunda encontra.
E a falha real acontece quando as duas leem "nao existe" antes de qualquer uma inserir, o que so
aparece com concorrencia de verdade contra o mesmo banco.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from credit_analysis.config import get_settings
from credit_analysis.domain.idempotencia import (
    JANELA,
    PRAZO_DE_ABANDONO,
    EstadoDaChave,
    impressao_do_pedido,
)
from credit_analysis.infrastructure.repositories.idempotencia import (
    RegistroIdempotenciaPostgres,
)

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

AGORA = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
LOCATARIO = "banco-x"


@pytest.fixture
async def pool() -> AsyncIterator[AsyncConnectionPool]:
    # `max_size=8` porque o teste de corrida precisa de conexoes simultaneas de verdade: com um
    # pool de uma conexao, as duas corrotinas se enfileirariam e a corrida nao aconteceria — o teste
    # passaria sem exercitar nada.
    p = AsyncConnectionPool(DSN, min_size=2, max_size=8, open=False)
    await p.open(wait=True, timeout=15)
    async with p.connection() as conexao:
        await conexao.execute("DELETE FROM idempotencia")
    yield p
    await p.close()


@pytest.fixture
def registro(pool: AsyncConnectionPool) -> RegistroIdempotenciaPostgres:
    return RegistroIdempotenciaPostgres(pool)


PEDIDO = {"valor": "45000.00", "prazo": 36}
IMPRESSAO = impressao_do_pedido(PEDIDO)


class TestReivindicacao:
    async def test_primeira_chamada_ganha_a_chave(
        self, registro: RegistroIdempotenciaPostgres
    ) -> None:
        resultado = await registro.reivindicar(LOCATARIO, "k1", IMPRESSAO, AGORA)

        assert resultado.reivindicada
        assert resultado.registro is None

    async def test_repeticao_depois_de_concluir_devolve_o_recurso(
        self, registro: RegistroIdempotenciaPostgres
    ) -> None:
        recurso = uuid4()
        await registro.reivindicar(LOCATARIO, "k1", IMPRESSAO, AGORA)
        await registro.concluir(LOCATARIO, "k1", recurso)

        resultado = await registro.reivindicar(LOCATARIO, "k1", IMPRESSAO, AGORA)

        assert not resultado.reivindicada
        assert resultado.registro is not None
        assert resultado.registro.estado is EstadoDaChave.CONCLUIDA
        assert resultado.registro.recurso_id == recurso

    async def test_repeticao_durante_o_processamento_e_concorrencia(
        self, registro: RegistroIdempotenciaPostgres
    ) -> None:
        """O estado intermediario existe para esta resposta.

        Sem `em_andamento`, a segunda chamada nao teria como distinguir "ja terminou, aqui esta o
        recurso" de "estou processando agora" — e devolver 404 ou criar outra analise seriam as duas
        saidas ruins.
        """
        await registro.reivindicar(LOCATARIO, "k1", IMPRESSAO, AGORA)

        resultado = await registro.reivindicar(LOCATARIO, "k1", IMPRESSAO, AGORA)

        assert not resultado.reivindicada
        assert resultado.registro is not None
        assert resultado.registro.estado is EstadoDaChave.EM_ANDAMENTO
        assert resultado.registro.recurso_id is None

    async def test_a_chave_e_por_locatario(self, registro: RegistroIdempotenciaPostgres) -> None:
        """Isto e seguranca, e nao organizacao.

        Com a chave global, um cliente que adivinhasse a chave de outro receberia **o recurso do
        outro** na repeticao: a idempotencia viraria um canal de leitura entre locatarios. Chave de
        idempotencia costuma ser UUID, mas costume nao e controle de acesso.
        """
        await registro.reivindicar("banco-x", "mesma-chave", IMPRESSAO, AGORA)

        resultado = await registro.reivindicar("banco-y", "mesma-chave", IMPRESSAO, AGORA)

        assert resultado.reivindicada, "a chave de um locatario bloqueou a do outro"


class TestCorrida:
    async def test_duas_reivindicacoes_simultaneas_so_uma_ganha(
        self, registro: RegistroIdempotenciaPostgres
    ) -> None:
        """O clique duplo, que chega junto — e o unico caso que o desenho ingenuo nao cobre.

        `SELECT` e depois `INSERT` tem uma janela entre as duas consultas: as duas requisicoes leem
        "nao existe", as duas inserem, as duas processam. `INSERT ... ON CONFLICT DO NOTHING
        RETURNING` decide no banco, numa operacao.

        `asyncio.gather` com o mesmo pool e o que produz a simultaneidade real; sem ele, a segunda
        chamada so comecaria depois de a primeira terminar, e o teste passaria com qualquer desenho.
        """
        resultados = await asyncio.gather(
            *(registro.reivindicar(LOCATARIO, "k-corrida", IMPRESSAO, AGORA) for _ in range(8))
        )

        vencedores = [r for r in resultados if r.reivindicada]
        assert len(vencedores) == 1, f"{len(vencedores)} reivindicacoes ganharam a mesma chave"

        # E as perdedoras sabem por que perderam.
        perdedoras = [r for r in resultados if not r.reivindicada]
        assert all(
            r.registro is not None and r.registro.estado is EstadoDaChave.EM_ANDAMENTO
            for r in perdedoras
        )

    async def test_chaves_diferentes_nao_disputam(
        self, registro: RegistroIdempotenciaPostgres
    ) -> None:
        """O par negativo: um `INSERT` que serializasse tudo passaria no teste de cima."""
        resultados = await asyncio.gather(
            *(registro.reivindicar(LOCATARIO, f"k{i}", IMPRESSAO, AGORA) for i in range(8))
        )

        assert all(r.reivindicada for r in resultados)


class TestAbandono:
    async def test_chave_abandonada_e_retomada(
        self, registro: RegistroIdempotenciaPostgres
    ) -> None:
        """Processo morre entre reivindicar e concluir. Sem retomada, a chave fica envenenada 24h.

        E o cliente que reenvia — comportamento esperado depois de um timeout — receberia 409 por um
        pedido que nunca foi processado.
        """
        await registro.reivindicar(LOCATARIO, "k1", IMPRESSAO, AGORA)

        depois = AGORA + PRAZO_DE_ABANDONO + timedelta(seconds=1)
        resultado = await registro.reivindicar(LOCATARIO, "k1", IMPRESSAO, depois)

        assert resultado.reivindicada

    async def test_chave_concluida_nao_e_retomada_por_idade(
        self, registro: RegistroIdempotenciaPostgres
    ) -> None:
        """A retomada vale so para `em_andamento`, e o limite importa.

        Se ela alcancasse chave concluida, uma repeticao dois minutos depois criaria a **segunda**
        analise — o defeito original, agora com um prazo em vez de sempre.
        """
        await registro.reivindicar(LOCATARIO, "k1", IMPRESSAO, AGORA)
        await registro.concluir(LOCATARIO, "k1", uuid4())

        depois = AGORA + PRAZO_DE_ABANDONO + timedelta(seconds=1)
        resultado = await registro.reivindicar(LOCATARIO, "k1", IMPRESSAO, depois)

        assert not resultado.reivindicada
        assert resultado.registro is not None
        assert resultado.registro.estado is EstadoDaChave.CONCLUIDA

    async def test_falha_libera_a_chave(self, registro: RegistroIdempotenciaPostgres) -> None:
        """Erro transitorio nao pode bloquear o retry por dois minutos."""
        await registro.reivindicar(LOCATARIO, "k1", IMPRESSAO, AGORA)
        await registro.liberar(LOCATARIO, "k1")

        resultado = await registro.reivindicar(LOCATARIO, "k1", IMPRESSAO, AGORA)

        assert resultado.reivindicada

    async def test_liberar_nao_apaga_chave_concluida(
        self, registro: RegistroIdempotenciaPostgres
    ) -> None:
        """A guarda `AND estado = 'em_andamento'` no `DELETE`, e por que ela nao e paranoia.

        Um `liberar` chamado por engano no caminho de sucesso — um `finally` mal colocado — apagaria
        a chave de um pedido concluido, e a repeticao criaria a segunda analise.
        """
        recurso = uuid4()
        await registro.reivindicar(LOCATARIO, "k1", IMPRESSAO, AGORA)
        await registro.concluir(LOCATARIO, "k1", recurso)

        await registro.liberar(LOCATARIO, "k1")

        resultado = await registro.reivindicar(LOCATARIO, "k1", IMPRESSAO, AGORA)
        assert not resultado.reivindicada
        assert resultado.registro is not None
        assert resultado.registro.recurso_id == recurso


class TestJanela:
    async def test_chave_fora_da_janela_pode_ser_reusada(
        self, registro: RegistroIdempotenciaPostgres
    ) -> None:
        """Passadas 24h, a mesma string vale como pedido novo.

        Sem isto, um cliente que derive a chave de algo estavel — numero de proposta, por exemplo —
        ficaria impedido de reenviar para sempre.
        """
        await registro.reivindicar(LOCATARIO, "k1", IMPRESSAO, AGORA)
        await registro.concluir(LOCATARIO, "k1", uuid4())

        depois = AGORA + JANELA + timedelta(minutes=1)
        resultado = await registro.reivindicar(LOCATARIO, "k1", IMPRESSAO, depois)

        assert resultado.reivindicada

    async def test_purga_remove_apenas_o_que_venceu(
        self, registro: RegistroIdempotenciaPostgres
    ) -> None:
        await registro.reivindicar(LOCATARIO, "velha", IMPRESSAO, AGORA)
        await registro.reivindicar(LOCATARIO, "nova", IMPRESSAO, AGORA + JANELA)

        removidas = await registro.purgar_vencidas(AGORA + JANELA + timedelta(minutes=1))

        assert removidas == 1
        # A nova continua la, e a repeticao dela ainda e reconhecida.
        assert not (
            await registro.reivindicar(LOCATARIO, "nova", IMPRESSAO, AGORA + JANELA)
        ).reivindicada


class TestImpressaoDoPedido:
    def test_ordem_das_chaves_nao_muda_a_impressao(self) -> None:
        """JSON equivalente e o mesmo pedido.

        Sem canonicalizar, um cliente que serialize com dicionario nao ordenado alternaria a
        impressao entre chamadas — e o retry legitimo viraria conflito.
        """
        assert impressao_do_pedido({"a": 1, "b": 2}) == impressao_do_pedido({"b": 2, "a": 1})

    def test_pedido_diferente_muda_a_impressao(self) -> None:
        assert impressao_do_pedido({"valor": "45000.00"}) != impressao_do_pedido(
            {"valor": "80000.00"}
        )

    def test_a_impressao_nao_contem_o_pedido(self) -> None:
        """SHA-256 e nao o corpo: ele tem dado pessoal, e este valor e comparado e registrado."""
        impressao = impressao_do_pedido({"cpf": "529.982.247-25", "nome": "Maria"})

        assert "529" not in impressao
        assert "Maria" not in impressao
        assert len(impressao) == 64

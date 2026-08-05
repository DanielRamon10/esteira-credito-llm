"""Registro de idempotencia em Postgres.

## Por que Postgres e nao um cache em memoria

Mesma licao da Camada 9. Com o registro em memoria, cada replica da API teria o seu: a primeira
chamada reivindicaria a chave na replica A, a repeticao cairia na B e criaria a segunda analise —
exatamente o defeito que esta camada existe para corrigir, agora com aparencia de resolvido.

Redis seria a escolha natural em producao e nao muda o desenho: o que importa e a reivindicacao ser
atomica e compartilhada. Postgres ja esta aqui, e `INSERT ... ON CONFLICT` da a atomicidade sem
somar uma dependencia.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import structlog
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from credit_analysis.domain.idempotencia import (
    JANELA,
    PRAZO_DE_ABANDONO,
    EstadoDaChave,
    RegistroDeIdempotencia,
)

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Reivindicacao:
    """O que a rota precisa saber depois de tentar reivindicar uma chave.

    Tres desfechos, e cada um leva a uma resposta diferente:

    - `reivindicada` — esta requisicao ganhou a chave e deve processar;
    - `registro` com estado `CONCLUIDA` — repeticao: ler o recurso e devolve-lo;
    - `registro` com estado `EM_ANDAMENTO` — outra requisicao esta processando agora: 409.

    Um booleano so nao daria conta: "nao reivindiquei" abrange repeticao e concorrencia, e as duas
    respostas sao diferentes para o cliente.
    """

    reivindicada: bool
    registro: RegistroDeIdempotencia | None


class RegistroIdempotenciaPostgres:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def reivindicar(
        self, locatario: str, chave: str, impressao: str, agora: datetime
    ) -> Reivindicacao:
        """Tenta tomar a chave para esta requisicao, de forma atomica.

        ## Por que `INSERT ... ON CONFLICT` e nao "consultar e depois inserir"

        O desenho ingenuo — `SELECT`, se nao existe entao `INSERT` — tem uma janela entre as duas
        consultas. Duas requisicoes simultaneas leem "nao existe", as duas inserem, as duas
        processam, e a idempotencia falha exatamente no caso que ela existe para cobrir: o clique
        duplo, que chega **junto**.

        `INSERT ... ON CONFLICT DO NOTHING RETURNING` decide no banco, numa operacao. Quem recebe
        linha de volta ganhou; quem recebe vazio perdeu, e ai le a linha do vencedor para saber se e
        repeticao ou concorrencia.

        ## A retomada de chave abandonada

        O segundo `UPDATE` cobre o processo que morreu entre reivindicar e concluir. Sem ele a chave
        ficaria envenenada pelas 24h da janela.

        A condicao `criada_em < %s` e avaliada **no `UPDATE`**, e nao lida antes: duas requisicoes
        tentando retomar a mesma chave abandonada disputariam a mesma linha, e o Postgres serializa
        — uma atualiza, a outra ve `rowcount = 0` e trata como concorrencia.
        """
        limite_da_janela = agora - JANELA
        limite_de_abandono = agora - PRAZO_DE_ABANDONO

        async with (
            self._pool.connection() as conexao,
            conexao.transaction(),
            conexao.cursor(row_factory=dict_row) as cursor,
        ):
            # Primeiro: limpa a chave se ela expirou. Sem isto, uma chave de ontem impediria o
            # cliente de reusar a mesma string amanha — e "reusar depois da janela" e o
            # comportamento documentado.
            await cursor.execute(
                """
                DELETE FROM idempotencia
                WHERE locatario = %s AND chave = %s AND criada_em < %s
                """,
                (locatario, chave, limite_da_janela),
            )

            await cursor.execute(
                """
                INSERT INTO idempotencia (locatario, chave, impressao, estado, criada_em)
                VALUES (%s, %s, %s, 'em_andamento', %s)
                ON CONFLICT (locatario, chave) DO NOTHING
                RETURNING chave
                """,
                (locatario, chave, impressao, agora),
            )
            if await cursor.fetchone() is not None:
                return Reivindicacao(reivindicada=True, registro=None)

            # Perdeu a corrida — ou a chave ja existia. Tenta retomar se foi abandonada.
            await cursor.execute(
                """
                UPDATE idempotencia
                SET impressao = %s, criada_em = %s
                WHERE locatario = %s AND chave = %s
                  AND estado = 'em_andamento' AND criada_em < %s
                RETURNING chave
                """,
                (impressao, agora, locatario, chave, limite_de_abandono),
            )
            if await cursor.fetchone() is not None:
                logger.warning(
                    "idempotencia.chave_retomada",
                    detalhe="reivindicacao anterior abandonada; provavel processo interrompido",
                )
                return Reivindicacao(reivindicada=True, registro=None)

            await cursor.execute(
                """
                SELECT locatario, chave, impressao, estado, recurso_id, criada_em
                FROM idempotencia WHERE locatario = %s AND chave = %s
                """,
                (locatario, chave),
            )
            linha = await cursor.fetchone()

        if linha is None:
            # Alcancavel por uma corrida com a purga: a chave existia no `INSERT`, e sumiu antes do
            # `SELECT`. Tratar como reivindicada faria duas analises; tratar como concorrencia faz o
            # cliente repetir, que e o desfecho seguro.
            logger.warning("idempotencia.chave_sumiu_na_leitura")
            return Reivindicacao(reivindicada=False, registro=None)

        return Reivindicacao(reivindicada=False, registro=_montar(dict(linha)))

    async def concluir(self, locatario: str, chave: str, recurso_id: UUID) -> None:
        """Marca a chave como concluida e amarra o recurso criado."""
        async with self._pool.connection() as conexao:
            await conexao.execute(
                """
                UPDATE idempotencia SET estado = 'concluida', recurso_id = %s
                WHERE locatario = %s AND chave = %s
                """,
                (recurso_id, locatario, chave),
            )

    async def liberar(self, locatario: str, chave: str) -> None:
        """Devolve a chave quando o processamento falhou.

        Sem isto, uma falha transitoria — banco fora do ar por um segundo, bureau em timeout —
        envenenaria a chave pelo prazo de abandono, e o retry do cliente receberia 409. Ou seja: a
        idempotencia transformaria um erro recuperavel num bloqueio de dois minutos.

        Apaga em vez de marcar `falhou`: um estado a mais precisaria de significado na repeticao, e
        o significado seria "tente de novo" — que e o que a ausencia da chave ja diz.
        """
        async with self._pool.connection() as conexao:
            await conexao.execute(
                "DELETE FROM idempotencia WHERE locatario = %s AND chave = %s AND estado = %s",
                (locatario, chave, EstadoDaChave.EM_ANDAMENTO.value),
            )

    async def purgar_vencidas(self, agora: datetime) -> int:
        """Remove chaves fora da janela. Chamado pelo job de purga da Camada 10."""
        async with self._pool.connection() as conexao:
            cursor = await conexao.execute(
                "DELETE FROM idempotencia WHERE criada_em < %s", (agora - JANELA,)
            )
            return int(cursor.rowcount)


def _montar(linha: dict[str, object]) -> RegistroDeIdempotencia:
    return RegistroDeIdempotencia(
        chave=str(linha["chave"]),
        locatario=str(linha["locatario"]),
        impressao=str(linha["impressao"]),
        estado=EstadoDaChave(str(linha["estado"])),
        recurso_id=linha["recurso_id"] if isinstance(linha["recurso_id"], UUID) else None,
        criada_em=linha["criada_em"],  # type: ignore[arg-type]
    )

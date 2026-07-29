"""Vector store em Postgres + pgvector.

Mesmo port que `VectorStoreMemoria`, outra implementacao. Trocar um pelo outro
e mudar uma linha no composition root — nenhum caso de uso sabe qual esta em
uso, e por isso a suite inteira roda contra o adapter em memoria enquanto os
testes de integracao exercitam este.

Por que Postgres e nao um banco vetorial dedicado: o corpus de politicas ja
precisa de transacao, backup, replica e controle de acesso — coisas que o time
de dados do banco ja opera para Postgres. pgvector entrega busca por
similaridade dentro dessa infraestrutura, sem introduzir um novo sistema com
seu proprio modelo de operacao. Um Qdrant/Weaviate se justifica quando o
volume ou o QPS deixam de caber; nao e o caso de um corpus de politicas.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import structlog
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from credit_analysis.domain.politica import (
    ReferenciaPolitica,
    TrechoPolitica,
    TrechoRecuperado,
)

logger = structlog.get_logger(__name__)

# Dimensao fixada no schema (coluna vector(1024)). Validar aqui faz o erro
# aparecer com mensagem util em vez de um erro de tipo do Postgres no meio de
# um INSERT em lote.
DIMENSAO_ESPERADA = 1024

_COLUNAS = """
    id, politica_id, versao, secao, titulo_politica, caminho_secao,
    texto, produtos, area, vigencia_inicio
"""

_UPSERT = f"""
INSERT INTO trecho_politica ({_COLUNAS}, embedding)
VALUES (
    %(id)s, %(politica_id)s, %(versao)s, %(secao)s, %(titulo_politica)s,
    %(caminho_secao)s, %(texto)s, %(produtos)s, %(area)s, %(vigencia_inicio)s,
    %(embedding)s
)
ON CONFLICT (id) DO UPDATE SET
    politica_id     = EXCLUDED.politica_id,
    versao          = EXCLUDED.versao,
    secao           = EXCLUDED.secao,
    titulo_politica = EXCLUDED.titulo_politica,
    caminho_secao   = EXCLUDED.caminho_secao,
    texto           = EXCLUDED.texto,
    produtos        = EXCLUDED.produtos,
    area            = EXCLUDED.area,
    vigencia_inicio = EXCLUDED.vigencia_inicio,
    embedding       = EXCLUDED.embedding,
    atualizado_em   = now()
"""

# `<=>` e o operador de distancia de cosseno do pgvector: 0 = identico.
# Convertemos para similaridade com 1 - distancia, para que o score tenha o
# mesmo significado do adapter em memoria e a fusao RRF compare iguais.
#
# O filtro por produto usa `cardinality(produtos) = 0 OR produtos && ARRAY[...]`:
# trecho sem produto declarado vale para todos, mesma semantica de
# `TrechoPolitica.aplicavel_a`.
_BUSCA = f"""
SELECT {_COLUNAS}, 1 - (embedding <=> %(consulta)s) AS similaridade
FROM trecho_politica
WHERE %(produto)s::text IS NULL
   OR cardinality(produtos) = 0
   OR produtos && ARRAY[%(produto)s]::text[]
ORDER BY embedding <=> %(consulta)s
LIMIT %(limite)s
"""


class VectorStorePgVector:
    """Indice de trechos persistido em Postgres."""

    def __init__(self, pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
        self._pool = pool

    async def indexar(
        self, trechos: Sequence[TrechoPolitica], vetores: Sequence[Sequence[float]]
    ) -> None:
        if len(trechos) != len(vetores):
            raise ValueError(
                f"Quantidade de trechos ({len(trechos)}) difere da de vetores ({len(vetores)})"
            )
        if not trechos:
            return

        for vetor in vetores:
            if len(vetor) != DIMENSAO_ESPERADA:
                raise ValueError(
                    f"Embedding com {len(vetor)} dimensoes; o schema espera "
                    f"{DIMENSAO_ESPERADA}. Trocar de modelo exige ALTER da coluna "
                    f"e reingestao completa."
                )

        registros = [
            {
                "id": trecho.id,
                "politica_id": trecho.referencia.politica_id,
                "versao": trecho.referencia.versao,
                "secao": trecho.referencia.secao,
                "titulo_politica": trecho.titulo_politica,
                "caminho_secao": list(trecho.caminho_secao),
                "texto": trecho.texto,
                "produtos": sorted(trecho.produtos),
                "area": trecho.area,
                "vigencia_inicio": trecho.vigencia_inicio,
                "embedding": _para_literal(vetor),
            }
            for trecho, vetor in zip(trechos, vetores, strict=True)
        ]

        # Uma transacao para o lote inteiro: ou o corpus e substituido por
        # completo, ou nada muda. Ingestao pela metade deixaria o indice
        # respondendo com uma mistura de duas versoes da politica.
        async with self._pool.connection() as conexao, conexao.cursor() as cursor:
            await cursor.executemany(_UPSERT, registros)

        logger.info("pgvector.indexado", trechos=len(registros))

    async def buscar_denso(
        self,
        vetor: Sequence[float],
        k: int = 5,
        produto: str | None = None,
    ) -> list[TrechoRecuperado]:
        if k <= 0:
            return []

        async with (
            self._pool.connection() as conexao,
            conexao.cursor(row_factory=dict_row) as cursor,
        ):
            await cursor.execute(
                _BUSCA,
                {"consulta": _para_literal(vetor), "produto": produto, "limite": k},
            )
            linhas = await cursor.fetchall()

        return [
            TrechoRecuperado(
                trecho=_para_trecho(linha),
                score=float(linha["similaridade"]),
                origem="denso",
            )
            for linha in linhas
        ]

    async def listar_todos(self) -> list[TrechoPolitica]:
        # `ORDER BY id` garante ordem estavel, o que mantem o indice BM25
        # derivado deste resultado reproduzivel entre processos.
        async with (
            self._pool.connection() as conexao,
            conexao.cursor(row_factory=dict_row) as cursor,
        ):
            await cursor.execute(f"SELECT {_COLUNAS} FROM trecho_politica ORDER BY id")
            linhas = await cursor.fetchall()

        return [_para_trecho(linha) for linha in linhas]

    async def contar(self) -> int:
        async with self._pool.connection() as conexao, conexao.cursor() as cursor:
            await cursor.execute("SELECT count(*) FROM trecho_politica")
            resultado = await cursor.fetchone()

        return int(resultado[0]) if resultado else 0

    async def limpar(self) -> None:
        """Esvazia o indice. Usado pela ingestao com `--recriar` e pelos testes."""
        async with self._pool.connection() as conexao, conexao.cursor() as cursor:
            await cursor.execute("TRUNCATE trecho_politica")


def _para_literal(vetor: Sequence[float]) -> str:
    """Serializa o vetor no formato textual que o pgvector aceita.

    Evita registrar o tipo do psycopg so para isso: o literal `[1,2,3]` e
    aceito no cast implicito para `vector` e nao acopla o adapter a mais uma
    peca de configuracao global.
    """
    return "[" + ",".join(repr(float(v)) for v in vetor) + "]"


def _para_trecho(linha: dict[str, Any]) -> TrechoPolitica:
    return TrechoPolitica(
        referencia=ReferenciaPolitica(
            politica_id=linha["politica_id"],
            versao=linha["versao"],
            secao=linha["secao"],
        ),
        titulo_politica=linha["titulo_politica"],
        caminho_secao=tuple(linha["caminho_secao"] or ()),
        texto=linha["texto"],
        produtos=frozenset(linha["produtos"] or ()),
        vigencia_inicio=linha["vigencia_inicio"],
        area=linha["area"] or "",
    )


def criar_pool(dsn: str, minimo: int = 1, maximo: int = 8) -> AsyncConnectionPool[Any]:
    """Cria o pool sem abrir conexao ainda.

    `open=False` porque abrir no construtor dispara I/O fora do event loop e o
    psycopg emite aviso. Quem cria chama `await pool.open()` no lifespan da
    aplicacao, onde ha loop rodando e onde o fechamento tambem esta garantido.
    """
    return AsyncConnectionPool(dsn, min_size=minimo, max_size=maximo, open=False)

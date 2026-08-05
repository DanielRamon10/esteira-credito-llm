"""Job de purga por prazo de retencao.

    python -m credit_analysis.purga_main

## Por que um processo proprio, e nao uma tarefa dentro da API

Purga apaga dado em lote. Dentro da API ela herdaria as permissoes da API e rodaria em cada replica
— tres replicas em producao, tres purgas concorrentes disputando as mesmas linhas.

Como job, ele roda uma vez por vez, tem o proprio agendamento, e — no dia em que o objeto no
armazenamento entrar no escopo (ver `RETENCAO_OBJETO`) — pode ter uma role com `DeleteObject` que a
API deliberadamente nao tem.

## Ele nao e um servico

Executa, informa o que fez, sai. Sem laco, sem servidor HTTP, sem sonda: quem repete e o
`CronJob` do Kubernetes, e transformar isto num processo de vida longa com `sleep(86400)` trocaria
um agendador testado por um `while True`.

Saida 0 sempre que a purga rodou, inclusive quando nao havia nada a purgar — "nada venceu hoje" e
sucesso, e um codigo diferente de zero faria o alerta do CronJob disparar toda noite ate alguem
desliga-lo.
"""

from __future__ import annotations

import structlog
from plataforma.logging import configurar_logging

from credit_analysis.application.use_cases.ciclo_de_vida import PurgarDadoVencido
from credit_analysis.config import Settings, get_settings
from credit_analysis.infrastructure.event_loop import executar
from credit_analysis.infrastructure.rag.pgvector_store import criar_pool
from credit_analysis.infrastructure.repositories.idempotencia import (
    RegistroIdempotenciaPostgres,
)
from credit_analysis.infrastructure.repositories.postgres import RepositorioAnalisesPostgres

logger = structlog.get_logger(__name__)


def _conferir_dependencias(settings: Settings) -> None:
    """Recusa rodar sem Postgres, com o motivo.

    Sem banco, `criar_app` monta o repositorio em memoria e a purga percorreria um dicionario vazio,
    saindo com `linhas=0` e codigo 0. O CronJob registraria sucesso todas as noites enquanto o dado
    vencido continuasse no banco — falha silenciosa numa rotina que existe para cumprir prazo legal.
    """
    if not settings.usar_pgvector:
        raise RuntimeError(
            "a purga exige CREDIT_POSTGRES_DSN. Sem banco ela percorreria um repositorio em "
            "memoria e reportaria sucesso com zero linhas, escondendo dado vencido no Postgres."
        )


def main() -> None:
    settings = get_settings()
    configurar_logging(nivel=settings.nivel_log, formato_json=settings.log_json)
    _conferir_dependencias(settings)

    # Pool minusculo: o job faz um `UPDATE` e sai. `maximo=2` deixa margem para o psycopg abrir uma
    # segunda conexao sem competir com a API pelas do banco.
    pool = criar_pool(settings.postgres_dsn, minimo=1, maximo=2)

    async def rodar() -> None:
        await pool.open(wait=True, timeout=30)
        try:
            resultado = await PurgarDadoVencido(
                RepositorioAnalisesPostgres(pool), RegistroIdempotenciaPostgres(pool)
            ).executar()
            logger.info(
                "purga.finalizada",
                textos_purgados=resultado.textos_purgados,
                chaves_purgadas=resultado.chaves_purgadas,
                limite=resultado.limite_aplicado.isoformat(),
            )
        finally:
            await pool.close()

    executar(rodar())


if __name__ == "__main__":
    main()

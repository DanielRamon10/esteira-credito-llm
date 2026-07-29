"""Ingestao do corpus de politicas no vector store.

Roda como comando, nao como parte do boot da API: indexar 37 trechos custa
segundos de inferencia ONNX, e repetir isso a cada replica que sobe e
desperdicio. A ingestao e um passo de deploy.

    python -m credit_analysis.ingestao --recriar

E idempotente: o upsert por `trecho.id` faz rodar duas vezes ter o mesmo
efeito de rodar uma. `--recriar` existe para o caso em que trechos foram
*removidos* do corpus — o upsert sozinho nao apaga o que sumiu do markdown.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import structlog
from plataforma.logging import configurar_logging

from credit_analysis.config import Settings, get_settings
from credit_analysis.infrastructure.event_loop import executar
from credit_analysis.infrastructure.rag.carregador import CorpusInvalido, carregar_corpus
from credit_analysis.infrastructure.rag.embeddings import EmbedderFastEmbed
from credit_analysis.infrastructure.rag.pgvector_store import VectorStorePgVector, criar_pool

logger = structlog.get_logger(__name__)


async def ingerir(settings: Settings, recriar: bool = False) -> int:
    """Carrega o corpus, vetoriza e grava no pgvector. Devolve quantos trechos."""
    if not settings.usar_pgvector:
        raise SystemExit(
            "CREDIT_POSTGRES_DSN nao configurado.\n"
            "Suba o banco com `docker compose up -d` e exporte, por exemplo:\n"
            "  CREDIT_POSTGRES_DSN=postgresql://credito:credito_local@localhost:5432/credito"
        )

    diretorio = _resolver(settings.diretorio_politicas)
    trechos = carregar_corpus(diretorio)
    logger.info("ingestao.corpus_carregado", trechos=len(trechos), diretorio=str(diretorio))

    embedder = EmbedderFastEmbed(settings.modelo_embedding)

    inicio = time.perf_counter()
    vetores = embedder.vetorizar([t.texto_para_indexar for t in trechos])
    logger.info(
        "ingestao.vetorizado",
        trechos=len(vetores),
        dimensoes=embedder.dimensoes,
        segundos=round(time.perf_counter() - inicio, 2),
    )

    pool = criar_pool(settings.postgres_dsn)
    await pool.open(wait=True, timeout=30)
    try:
        store = VectorStorePgVector(pool)

        if recriar:
            await store.limpar()
            logger.info("ingestao.indice_limpo")

        await store.indexar(trechos, vetores)
        total = await store.contar()
    finally:
        await pool.close()

    logger.info("ingestao.concluida", total_no_indice=total)
    return total


def _resolver(diretorio: Path) -> Path:
    """Aceita caminho relativo ao cwd ou a raiz do servico."""
    if diretorio.is_absolute() and diretorio.is_dir():
        return diretorio
    if diretorio.is_dir():
        return diretorio.resolve()

    # `parents[2]` a partir de src/credit_analysis/ingestao.py = raiz do servico.
    candidato = Path(__file__).parents[2] / diretorio
    if candidato.is_dir():
        return candidato

    raise CorpusInvalido(f"Diretorio de politicas nao encontrado: {diretorio}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Indexa o corpus de politicas no pgvector")
    parser.add_argument(
        "--recriar",
        action="store_true",
        help="esvazia o indice antes de gravar (use quando trechos foram removidos)",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    configurar_logging(nivel=settings.nivel_log, formato_json=False)

    try:
        total = executar(ingerir(settings, recriar=args.recriar))
    except CorpusInvalido as exc:
        logger.error("ingestao.corpus_invalido", erro=str(exc))
        return 2
    except Exception as exc:
        logger.error("ingestao.falhou", erro=str(exc), exc_info=True)
        return 1

    print(f"OK: {total} trechos indexados")
    return 0


if __name__ == "__main__":
    sys.exit(main())

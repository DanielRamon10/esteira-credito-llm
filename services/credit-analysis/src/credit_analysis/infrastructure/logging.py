"""Logging estruturado com structlog.

Log em JSON com chaves estaveis, nao string interpolada. A diferenca importa
quando a Camada 5 mandar isso para Loki/CloudWatch: `analise_id` como campo e
consultavel, dentro de uma frase nao e.

Em desenvolvimento o renderer vira console colorido, porque JSON puro no
terminal e ilegivel.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


def configurar_logging(nivel: str = "INFO", formato_json: bool = True) -> None:
    """Configura structlog e a stdlib de uma vez.

    Bibliotecas de terceiros logam pela stdlib; sem redirecionar o root logger
    metade das linhas sairia em outro formato.
    """
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=nivel.upper())

    processadores: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if formato_json:
        processadores.append(structlog.processors.JSONRenderer())
    else:
        processadores.append(structlog.dev.ConsoleRenderer(colors=True))

    structlog.configure(
        processors=processadores,
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[nivel.upper()]
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

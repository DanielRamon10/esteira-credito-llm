"""Logging estruturado.

Duplica o modulo do `credit-analysis`, e a duplicacao esta anotada como divida no
README do monorepo. **Este** e um caso em que compartilhar vale: e infraestrutura
tecnica, nao dominio, e nao ha razao para dois servicos formatarem log de formas
diferentes. Fica para a extracao quando o terceiro servico existir e mostrar o que
de fato se repete — extrair agora seria adivinhar a abstracao com um consumidor so.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


def configurar_logging(nivel: str = "INFO", formato_json: bool = True) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=nivel.upper())

    processadores: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processadores.append(
        structlog.processors.JSONRenderer() if formato_json else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=processadores,
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[nivel.upper()]
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

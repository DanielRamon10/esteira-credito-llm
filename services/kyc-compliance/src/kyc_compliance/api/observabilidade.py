"""Traducao de resultado de dominio em metrica.

Mesma disciplina do outro servico: `domain` e `application` nao conhecem
`prometheus_client`. A traducao mora na borda, e uma falha ao medir nunca derruba o
que foi medido — uma triagem concluida com sucesso nao pode virar erro porque um
contador teve problema.
"""

from __future__ import annotations

import structlog

from kyc_compliance.domain.triagem import Triagem
from kyc_compliance.infrastructure import metricas

logger = structlog.get_logger(__name__)


def registrar_triagem(triagem: Triagem, duracao_segundos: float) -> None:
    try:
        metricas.triagens.labels(
            decisao=triagem.decisao.value, nivel_risco=triagem.nivel_risco.value
        ).inc()
        metricas.duracao.observe(duracao_segundos)
        metricas.entradas_avaliadas.observe(triagem.entradas_avaliadas)

        for correspondencia in triagem.correspondencias:
            # O tipo da lista nao esta na correspondencia (ela e do dominio de
            # matching, que nao conhece lista restritiva). Aqui basta o nivel; a
            # separacao por tipo sai da decisao, que ja carrega essa informacao.
            metricas.correspondencias.labels(
                nivel=correspondencia.nivel.value, tipo_lista="agregado"
            ).inc()
    except Exception:
        logger.warning("metricas.falha_ao_registrar", origem="triagem", exc_info=True)

"""Endpoint de metricas para o Prometheus raspar.

Fora do `prefixo_api` de proposito. `/metrics` na raiz e a convencao que todo
scrape config, ServiceMonitor e chart do ecossistema assume por padrao; esconder
atras de `/v1` obrigaria configuracao especial em cada lugar, sem ganho nenhum.

Nao aparece no OpenAPI: o consumidor e o Prometheus, nao um cliente da API, e
poluir a documentacao com uma rota que devolve texto de exposicao confunde quem
le o contrato.
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from credit_analysis.infrastructure.observabilidade.metricas import REGISTRO

router = APIRouter(tags=["Observabilidade"])


@router.get("/metrics", include_in_schema=False)
async def metricas() -> Response:
    """Exposicao no formato texto do Prometheus.

    Nota sobre multiplos workers: com mais de um processo Uvicorn no mesmo
    container, cada worker teria seu proprio registry e o scrape veria apenas um
    deles, por sorteio. A saida e `PROMETHEUS_MULTIPROC_DIR` com o
    `MultiProcessCollector`. Aqui o servico roda com um worker e escala por
    replica no Kubernetes, onde cada pod e raspado individualmente — que e o
    modelo mais simples e o que o Prometheus prefere.
    """
    return Response(content=generate_latest(REGISTRO), media_type=CONTENT_TYPE_LATEST)

"""Endpoint de metricas para o Prometheus raspar.

Fora do `prefixo_api` porque `/metrics` na raiz e a convencao que todo scrape config
e ServiceMonitor assume. Fora do OpenAPI porque o consumidor e o Prometheus, nao um
cliente da API.
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from kyc_compliance.infrastructure.metricas import REGISTRO

router = APIRouter(tags=["Observabilidade"])


@router.get("/metrics", include_in_schema=False)
async def metricas() -> Response:
    return Response(content=generate_latest(REGISTRO), media_type=CONTENT_TYPE_LATEST)

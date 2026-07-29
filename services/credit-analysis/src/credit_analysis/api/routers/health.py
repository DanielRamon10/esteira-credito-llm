"""Endpoints de saude.

Dois endpoints distintos porque o Kubernetes faz duas perguntas diferentes:
- /health (liveness): o processo esta vivo? Se falhar, reinicia o pod.
- /ready (readiness): da para mandar trafego? Se falhar, tira do load balancer
  mas nao reinicia.

Colapsar os dois em um endpoint so causa restart loop: uma dependencia lenta
derruba o liveness, o pod reinicia, sobe frio, falha de novo.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from credit_analysis.api.deps import SettingsDep
from credit_analysis.api.schemas import HealthResponse

router = APIRouter(tags=["Observabilidade"])


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
async def health(settings: SettingsDep) -> HealthResponse:
    """Nao toca em nenhuma dependencia externa de proposito."""
    return HealthResponse(
        status="ok",
        servico=settings.nome_servico,
        versao=settings.versao,
        ambiente=settings.ambiente.value,
    )


@router.get("/ready", response_model=HealthResponse, summary="Readiness probe")
async def ready(settings: SettingsDep, response: Response) -> HealthResponse:
    """Verifica as dependencias necessarias para servir trafego.

    Na Camada 1 a unica dependencia e o repositorio em memoria, sempre pronto.
    Camada 2 adiciona o vector store; Camada 6, o Postgres.
    """
    dependencias_ok = True

    if not dependencias_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthResponse(
        status="ok" if dependencias_ok else "degradado",
        servico=settings.nome_servico,
        versao=settings.versao,
        ambiente=settings.ambiente.value,
    )

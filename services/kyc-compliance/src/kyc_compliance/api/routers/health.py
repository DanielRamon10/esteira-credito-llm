"""Sondas de saude.

`/ready` confere a lista carregada, e essa e a diferenca em relacao a um readiness
generico: um pod com zero entradas responderia toda triagem com "nenhuma
correspondencia", aprovando todo mundo. Melhor sair do load balancer.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from kyc_compliance.api.deps import ListasDep, SettingsDep
from kyc_compliance.api.schemas import HealthResponse

router = APIRouter(tags=["Observabilidade"])


@router.get(
    "/health",
    response_model=HealthResponse,
    # `exclude_none` porque esta sonda nao consulta o repositorio: sem ele, os defaults
    # do schema fariam o corpo afirmar `entradas_carregadas: 0` — a condicao mais grave
    # do dominio — num endpoint que nunca teve esse numero para dar.
    response_model_exclude_none=True,
    summary="Liveness probe",
)
async def health(settings: SettingsDep) -> HealthResponse:
    """Nao toca em dependencia externa de proposito.

    Uma sonda de liveness que consulta dependencia transforma lentidao da dependencia em
    restart loop: o pod morre, sobe frio, falha de novo. Por isso ela nao sabe quantas
    entradas ha — e o corpo omite o campo em vez de inventar zero.
    """
    return HealthResponse(
        status="ok",
        servico=settings.nome_servico,
        versao=settings.versao,
        ambiente=settings.ambiente.value,
    )


@router.get("/ready", response_model=HealthResponse, summary="Readiness probe")
async def ready(settings: SettingsDep, listas: ListasDep, response: Response) -> HealthResponse:
    pronto = listas.total > 0

    if not pronto:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthResponse(
        status="ok" if pronto else "degradado",
        servico=settings.nome_servico,
        versao=settings.versao,
        ambiente=settings.ambiente.value,
        entradas_carregadas=listas.total,
        procedencia_listas=listas.procedencia,
    )

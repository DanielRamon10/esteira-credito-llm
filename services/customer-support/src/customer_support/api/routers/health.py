"""Sondas de saude."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from customer_support.api.deps import ConhecimentoDep, SettingsDep
from customer_support.api.schemas import HealthResponse

router = APIRouter(tags=["Observabilidade"])


@router.get(
    "/health",
    response_model=HealthResponse,
    # `exclude_none` porque esta sonda nao consulta a base: sem ele, os defaults do schema
    # fariam o corpo afirmar `artigos_publicos: 0` — a condicao que reprova o readiness —
    # num endpoint que nunca teve esse numero para dar.
    response_model_exclude_none=True,
    summary="Liveness probe",
)
async def health(settings: SettingsDep) -> HealthResponse:
    """Nao toca na base de conhecimento de proposito.

    Sonda de liveness que consulta dependencia transforma lentidao da dependencia em
    restart loop: o pod morre, sobe frio, falha de novo.
    """
    return HealthResponse(
        status="ok",
        servico=settings.nome_servico,
        versao=settings.versao,
        ambiente=settings.ambiente.value,
    )


@router.get("/ready", response_model=HealthResponse, summary="Readiness probe")
async def ready(
    settings: SettingsDep, conhecimento: ConhecimentoDep, request: Request, response: Response
) -> HealthResponse:
    """Exige artigo PUBLICO carregado, e nao apenas artigo.

    Uma base com apenas artigos internos responderia toda duvida com "nao encontrei",
    porque a busca do cliente filtra por visibilidade. O pod estaria vivo, a base
    carregada, e o servico inutil — o tipo de estado que so um readiness especifico
    detecta.
    """
    publicos = getattr(conhecimento, "publicos", conhecimento.total)
    pronto = publicos > 0

    if not pronto:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    llm = getattr(request.app.state, "llm", None)

    return HealthResponse(
        status="ok" if pronto else "degradado",
        servico=settings.nome_servico,
        versao=settings.versao,
        ambiente=settings.ambiente.value,
        artigos_carregados=conhecimento.total,
        artigos_publicos=publicos,
        llm=llm.identificacao if llm else "artigo",
    )

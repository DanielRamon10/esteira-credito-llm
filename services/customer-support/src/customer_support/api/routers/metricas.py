"""Endpoint de metricas para o Prometheus raspar."""

from __future__ import annotations

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from customer_support.infrastructure.metricas import REGISTRO

router = APIRouter(tags=["Observabilidade"])


@router.get("/metrics", include_in_schema=False)
async def metricas() -> Response:
    return Response(content=generate_latest(REGISTRO), media_type=CONTENT_TYPE_LATEST)

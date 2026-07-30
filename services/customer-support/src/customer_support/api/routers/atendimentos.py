"""Endpoint de atendimento."""

from __future__ import annotations

import time

from fastapi import APIRouter, status

from customer_support.api.deps import AtenderDep
from customer_support.api.observabilidade import registrar_atendimento
from customer_support.api.schemas import AtendimentoResponse, ErroResponse, PerguntaRequest
from customer_support.application.use_cases.atender import ComandoAtender

router = APIRouter(prefix="/atendimentos", tags=["Atendimento"])


@router.post(
    "",
    response_model=AtendimentoResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Responde a mensagem de um cliente",
    responses={422: {"model": ErroResponse}},
)
async def atender(payload: PerguntaRequest, caso: AtenderDep) -> AtendimentoResponse:
    """Classifica, roteia e responde.

    Sincrono. Com o modelo pequeno a resposta sai em ~15s, e sem modelo (origem
    `artigo`) em milissegundos. Nao ha o problema de latencia do agente do
    `credit-analysis`, que precisa de varias rodadas de inferencia.
    """
    inicio = time.perf_counter()
    resposta = await caso.executar(ComandoAtender(mensagem=payload.mensagem))
    registrar_atendimento(resposta, time.perf_counter() - inicio)
    return AtendimentoResponse.de_dominio(resposta)

"""Rotas de consulta ao corpus de politicas (RAG)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query

from credit_analysis.api.deps import FundamentarDep, RetrieverDep
from credit_analysis.api.schemas import (
    ConsultaPoliticaRequest,
    ErroResponse,
    FundamentacaoResponse,
    TrechoRecuperadoResponse,
)
from credit_analysis.application.use_cases.fundamentar_parecer import ComandoFundamentar
from credit_analysis.infrastructure.rag.retriever import ConfiguracaoBusca

router = APIRouter(prefix="/politicas", tags=["Politicas internas"])

_RESPOSTA_503: dict[int | str, dict[str, Any]] = {
    503: {"model": ErroResponse, "description": "Indice de politicas indisponivel"}
}


@router.get(
    "/buscar",
    response_model=list[TrechoRecuperadoResponse],
    summary="Buscar trechos de politica",
    responses=_RESPOSTA_503,
)
async def buscar(
    retriever: RetrieverDep,
    q: Annotated[str, Query(min_length=3, description="Pergunta em linguagem natural")],
    k: Annotated[int, Query(ge=1, le=20)] = 5,
    produto: Annotated[str | None, Query(description="Filtra por produto aplicavel")] = None,
) -> list[TrechoRecuperadoResponse]:
    """Retrieval puro, sem LLM.

    Serve para depurar: quando a fundamentacao sai ruim, este endpoint diz se o
    problema esta na busca ou na geracao. Sem ele, os dois erros parecem iguais.
    """
    resultados = await retriever.buscar(q, ConfiguracaoBusca(k=k, produto=produto))
    return [TrechoRecuperadoResponse.de_dominio(r) for r in resultados]


@router.post(
    "/consultar",
    response_model=FundamentacaoResponse,
    summary="Perguntar as politicas internas, com citacoes verificadas",
    responses=_RESPOSTA_503,
)
async def consultar(
    payload: ConsultaPoliticaRequest, caso: FundamentarDep
) -> FundamentacaoResponse:
    """Responde com base no corpus, citando politica, versao e secao.

    `citacoes_rejeitadas` traz o que o modelo alegou mas nao pode ser
    confirmado nos trechos recuperados. Exposto de proposito: filtrar em
    silencio esconderia a taxa de alucinacao de quem precisa medi-la.
    """
    fundamentacao = await caso.executar(
        ComandoFundamentar(pergunta=payload.pergunta, produto=payload.produto)
    )
    return FundamentacaoResponse.de_dominio(fundamentacao)

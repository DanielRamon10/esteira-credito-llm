"""Rotas de analise de credito."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, status

from credit_analysis.api.deps import AnalisarDep, ConsultarDep, ListarDep
from credit_analysis.api.observabilidade import registrar_parecer
from credit_analysis.api.schemas import (
    AnaliseRequest,
    AnaliseResponse,
    ErroResponse,
    PaginaAnalises,
)
from credit_analysis.api.seguranca import (
    ANALISES_ESCREVER,
    ANALISES_LER,
    Escopo,
)
from credit_analysis.application.use_cases.analisar_credito import ComandoAnalisar
from credit_analysis.domain.value_objects import Dinheiro

router = APIRouter(prefix="/analises", tags=["Analises de credito"])

# Escopo declarado **por rota**, e nao um `dependencies=[...]` no router inteiro.
#
# Um escopo unico no router forcaria `analises:ler` e `analises:escrever` a serem o mesmo, e
# a granularidade e justamente o ponto: o canal que cria proposta nao precisa poder ler
# proposta alheia, e o painel de analista que le nao precisa poder criar.


@router.post(
    "",
    response_model=AnaliseResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submeter uma analise de credito",
    dependencies=[Depends(Escopo(ANALISES_ESCREVER))],
    responses={
        401: {"model": ErroResponse, "description": "Credencial ausente ou invalida"},
        403: {"model": ErroResponse, "description": "Sem o escopo analises:escrever"},
        422: {"model": ErroResponse, "description": "Payload invalido"},
        500: {"model": ErroResponse, "description": "Erro interno"},
    },
)
async def criar_analise(payload: AnaliseRequest, caso: AnalisarDep) -> AnaliseResponse:
    """Executa a esteira de analise e devolve o parecer.

    Sincrono na Camada 1 porque o motor de score e puro CPU e responde em
    milissegundos. Quando OCR e LLM entrarem (Camadas 3 e 4) a latencia passa
    de segundos e este endpoint vira 202 Accepted + polling.
    """
    comando = ComandoAnalisar(
        solicitante=payload.solicitante.para_dominio(),
        proposta=payload.proposta.para_dominio(),
        renda_comprovada=(
            Dinheiro(payload.renda_comprovada) if payload.renda_comprovada is not None else None
        ),
        meses_historico_bancario=payload.meses_historico_bancario,
    )
    analise = await caso.executar(comando)
    registrar_parecer(analise)
    return AnaliseResponse.de_dominio(analise)


@router.get(
    "/{analise_id}",
    response_model=AnaliseResponse,
    summary="Consultar uma analise",
    dependencies=[Depends(Escopo(ANALISES_LER))],
    responses={
        401: {"model": ErroResponse, "description": "Credencial ausente ou invalida"},
        403: {"model": ErroResponse, "description": "Sem o escopo analises:ler"},
        404: {"model": ErroResponse, "description": "Analise nao encontrada"},
    },
)
async def consultar_analise(
    caso: ConsultarDep,
    analise_id: Annotated[UUID, Path(description="Identificador da analise")],
) -> AnaliseResponse:
    return AnaliseResponse.de_dominio(await caso.executar(analise_id))


@router.get(
    "",
    response_model=PaginaAnalises,
    summary="Listar analises",
    dependencies=[Depends(Escopo(ANALISES_LER))],
)
async def listar_analises(
    caso: ListarDep,
    limite: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PaginaAnalises:
    itens, total = await caso.executar(limite=limite, offset=offset)
    return PaginaAnalises(
        itens=[AnaliseResponse.de_dominio(a) for a in itens],
        total=total,
        limite=limite,
        offset=offset,
    )

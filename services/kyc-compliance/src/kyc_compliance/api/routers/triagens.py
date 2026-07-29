"""Endpoints de triagem."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status

from kyc_compliance.api.deps import ConsultarDep, ListarDep, TriarDep
from kyc_compliance.api.schemas import (
    ErroResponse,
    PaginaTriagens,
    TriagemRequest,
    TriagemResponse,
)
from kyc_compliance.application.use_cases.triar_cliente import ComandoTriar

router = APIRouter(prefix="/triagens", tags=["Triagem de KYC"])


@router.post(
    "",
    response_model=TriagemResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Tria um cliente contra as listas restritivas",
    responses={422: {"model": ErroResponse}},
)
async def triar(payload: TriagemRequest, caso: TriarDep) -> TriagemResponse:
    """Sincrono, e aqui isso nao e concessao.

    A triagem e comparacao em memoria, na casa de milissegundos: nao ha inferencia
    de modelo, entao nao existe o problema de latencia que torna o endpoint de
    agente do outro servico questionavel em produto.
    """
    triagem = await caso.executar(ComandoTriar(nome=payload.nome, cpf=payload.cpf))
    return TriagemResponse.de_dominio(triagem)


@router.get("/{triagem_id}", response_model=TriagemResponse, summary="Consulta uma triagem")
async def consultar(triagem_id: UUID, caso: ConsultarDep) -> TriagemResponse:
    triagem = await caso.executar(triagem_id)
    if triagem is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "codigo": "triagem_nao_encontrada",
                "mensagem": "Triagem nao encontrada",
            },
        )
    return TriagemResponse.de_dominio(triagem)


@router.get("", response_model=PaginaTriagens, summary="Lista triagens")
async def listar(
    caso: ListarDep,
    limite: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> PaginaTriagens:
    itens, total = await caso.executar(limite=limite, offset=offset)
    return PaginaTriagens(
        itens=[TriagemResponse.de_dominio(t) for t in itens],
        total=total,
        limite=limite,
        offset=offset,
    )
